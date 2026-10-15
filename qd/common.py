"""Model, probe and event helpers shared by every stage."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import torch

RESULTS = Path(os.environ.get("QD_RESULTS", "results"))
RESULTS.mkdir(parents=True, exist_ok=True)
EVENTS = RESULTS / "events.jsonl"
_T0 = time.time()


def event(stage: str, msg: str, emoji: str = "⚡", **kw) -> None:
    """Append one line to the event log that the live monitor streams."""
    rec = {"ts": time.time(), "src": "pod", "stage": stage, "emoji": emoji, "msg": msg, **kw}
    with open(EVENTS, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"{emoji} [{stage}] {msg} {json.dumps(kw, ensure_ascii=False) if kw else ''}", flush=True)


def gpu_stats() -> dict:
    if not torch.cuda.is_available():
        return {}
    free, total = torch.cuda.mem_get_info()
    return {"vram_used_gb": round((total - free) / 1e9, 1), "vram_total_gb": round(total / 1e9, 1)}


def write_jsonl(path: Path, rows) -> None:
    with open(path, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]


class QModel:
    """Qwen3.5 chat model with helpers for rendering, generation, activations and hooks."""

    def __init__(self, name: str, probe_key: str | None, device: str = "cuda"):
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self.name = name
        self.tok = AutoTokenizer.from_pretrained(name)
        self.tok.padding_side = "left"
        self.model = AutoModelForCausalLM.from_pretrained(name, dtype=torch.bfloat16, device_map=device)
        self.model.eval()
        self.device = device
        self.layers = self.model.model.layers
        self.n_layers = len(self.layers)
        self.im_start = self.tok.convert_tokens_to_ids("<|im_start|>")
        self.im_end = self.tok.convert_tokens_to_ids("<|im_end|>")
        self.user_tok = self.tok.encode("user", add_special_tokens=False)[0]
        self.probes = {}
        if probe_key:
            from probe_inference import load_probe_from_hub

            for arch in ("linear", "mlp", "efc", "axial"):
                self.probes[arch] = load_probe_from_hub(f"{probe_key}/{arch}").to(device)
            self.probe_layers = sorted({l for p in self.probes.values() for l in p.layers})
        self._hooks = []

    # ------------------------------------------------------------------ rendering
    def prompt_ids(self, messages, prefill: str = "", thinking: bool = False) -> list[int]:
        s = self.tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=True, enable_thinking=thinking)
        return self.tok.encode(s + prefill, add_special_tokens=False)

    def transcript(self, messages, answer: str):
        """Token ids and masks for messages (ending in a user turn) + an assistant answer."""
        p = self.prompt_ids(messages)
        c = self.tok.encode(answer, add_special_tokens=False) + [self.im_end]
        ids = p + c
        starts = [i for i in range(len(p) - 1) if p[i] == self.im_start and p[i + 1] == self.user_tok]
        prompt_mask = torch.tensor([True] * len(p) + [False] * len(c))
        completion_mask = ~prompt_mask
        return torch.tensor(ids), prompt_mask, completion_mask, torch.tensor(starts[-1])

    # ------------------------------------------------------------------ generation
    @torch.no_grad()
    def generate(self, batch, max_new_tokens: int = 300, thinking: bool = False, bs: int = 16, on_batch=None,
                 temperature: float = 0.0):
        """batch: list of dict(messages=..., prefill=...). Returns generated strings (greedy unless temperature > 0)."""
        sampling = dict(do_sample=True, temperature=temperature, top_p=0.95) if temperature > 0 else dict(do_sample=False)
        outs = []
        for i in range(0, len(batch), bs):
            chunk = batch[i:i + bs]
            seqs = [self.prompt_ids(b["messages"], b.get("prefill", ""), thinking) for b in chunk]
            L = max(len(s) for s in seqs)
            pad = self.tok.pad_token_id if self.tok.pad_token_id is not None else self.im_end
            ids = torch.tensor([[pad] * (L - len(s)) + s for s in seqs], device=self.device)
            att = torch.tensor([[0] * (L - len(s)) + [1] * len(s) for s in seqs], device=self.device)
            t = time.time()
            gen = self.model.generate(input_ids=ids, attention_mask=att, max_new_tokens=max_new_tokens,
                                      pad_token_id=pad, eos_token_id=[self.im_end], **sampling)
            new = gen[:, L:]
            ntok = int((new != pad).sum())
            for row in new:
                row = row.tolist()
                if self.im_end in row:
                    row = row[:row.index(self.im_end)]
                outs.append(self.tok.decode(row, skip_special_tokens=False).replace("<|endoftext|>", "").strip())
            if on_batch:
                on_batch(len(outs), ntok, ntok / max(time.time() - t, 1e-6))
        return outs

    # ------------------------------------------------------------------ activations + probes
    @torch.no_grad()
    def acts(self, ids: torch.Tensor) -> dict[int, torch.Tensor]:
        out = self.model(input_ids=ids.unsqueeze(0).to(self.device), output_hidden_states=True)
        return {l: out.hidden_states[l + 1][0] for l in self.probe_layers}

    @torch.no_grad()
    def probe_scores(self, messages, answer: str) -> dict[str, float]:
        ids, pm, cm, st = self.transcript(messages, answer)
        a = self.acts(ids)
        res = {}
        for arch, p in self.probes.items():
            try:
                mask = p.read_mask(pm, cm, st).to(self.device)
                res[arch] = float(p.score(a, mask))
            except ValueError as e:  # e.g. a one-token answer for linear/mlp
                res[arch] = None
                res[arch + "_err"] = str(e)[:120]
        return res

    # ------------------------------------------------------------------ hooks
    def clear_hooks(self):
        for h in self._hooks:
            h.remove()
        self._hooks = []

    def _hook(self, fn):
        def hook(module, args, output):
            if isinstance(output, tuple):
                return (fn(output[0]),) + tuple(output[1:])
            return fn(output)
        return hook

    def add_ablation(self, direction: torch.Tensor):
        d = (direction / direction.norm()).to(self.device, torch.bfloat16)

        def fn(h):
            return h - (h @ d).unsqueeze(-1) * d

        for layer in self.layers:
            self._hooks.append(layer.register_forward_hook(self._hook(fn)))

    def add_steering(self, layer_idx: int, vector: torch.Tensor):
        v = vector.to(self.device, torch.bfloat16)
        self._hooks.append(self.layers[layer_idx].register_forward_hook(self._hook(lambda h: h + v)))

    # ------------------------------------------------------------------ last-prompt-token states
    @torch.no_grad()
    def last_token_states(self, messages) -> torch.Tensor:
        ids = torch.tensor(self.prompt_ids(messages), device=self.device).unsqueeze(0)
        out = self.model(input_ids=ids, output_hidden_states=True)
        return torch.stack([h[0, -1].float().cpu() for h in out.hidden_states[1:]])  # (n_layers, d)

    @torch.no_grad()
    def logit_lens(self, messages, prefix: str, cands: list[str]) -> list[dict]:
        ids = torch.tensor(self.prompt_ids(messages, prefill=prefix), device=self.device).unsqueeze(0)
        out = self.model(input_ids=ids, output_hidden_states=True)
        first = {c: self.tok.encode(c, add_special_tokens=False)[0] for c in cands}
        rows = []
        for li, h in enumerate(out.hidden_states[1:]):
            x = h[0, -1]
            if li < self.n_layers - 1:  # the last hidden state is already normed
                x = self.model.model.norm(x)
            probs = torch.softmax(self.model.lm_head(x).float(), -1)
            rows.append({c: float(probs[t]) for c, t in first.items()})
        return rows
