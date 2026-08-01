#!/usr/bin/env python3
"""Verifier verdicts and shot plans must not be parsed by accident.

Why this file exists. Two parsers in the pipe decided real outcomes from the SHAPE of an LLM reply,
and both failed in the direction that hides the failure.

`_verify_image` scored a pass as the ABSENCE of a substring:

    ok = not re.search(r"^\\s*OK:\\s*no\\b", out, re.I | re.M)

so every reply that did not literally contain a line starting `OK: no` was a pass — markdown
emphasis, a preamble that pushed the verdict past num_predict, or the model answering the question
correctly in prose. That last one is not hypothetical: measured on this box, the same vision model
asked a colour question with no format constraint returned `The user wants me to identify the
dominant color of the provided image.` and nothing else, inside a 60-token budget. Under the old
rule that was a PASS, and the QA loop reported the image as correct.

`_plan_shots` carved JSON with `re.search(r"\\[.*\\]", out, re.S)` — greedy over DOTALL, so a reply
whose commentary contained a later `]` swallowed everything between. On failure the caller silently
generated a single 5-second clip in place of the requested multi-shot sequence, with no message.

Both are offline and deterministic — no GPU, no Ollama.

Usage:  python3 tests/test_structured_parse.py [pipe_path]
"""
import importlib.util, sys

PIPE_PATH = sys.argv[1] if len(sys.argv) > 1 else "/home/ohmz/ai-stack/pipes/live/auto_assistant.py"
spec = importlib.util.spec_from_file_location("aa_parse", PIPE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail and not ok else ""))


def main():
    p = mod.Pipe()
    p._metric = lambda **kw: metrics.append(kw)      # capture instead of writing the jsonl
    metrics = []
    verdict = p._parse_verdict

    print("--- a verdict must be STATED, not inferred from what is missing ---")
    for label, raw, want in [
        ("plain no", "OK: no\nFIX: remove the extra boy", (False, "remove the extra boy")),
        ("plain yes", "OK: yes\nFIX: none", (True, "")),
        ("markdown emphasis", "**OK:** no\n**FIX:** remove the hat", (False, "remove the hat")),
        ("preamble before the verdict",
         "Sure! Here is my check.\nOK: no\nFIX: make the sky blue", (False, "make the sky blue")),
        ("truncated FIX is still actionable",
         "OK: no\nFIX: Remove the boy in the blu", (False, "Remove the boy in the blu")),
    ]:
        got = verdict(raw)
        check(f"{label} -> {want}", got == want, repr(got))

    print("--- the case that was a silent false PASS ---")
    metrics.clear()
    got = verdict("No, the image does not contain a basketball.")
    # Still open — the QA loop must never discard a correct render on a parse miss — but COUNTED.
    check("prose 'No, ...' no longer reads as a verdict", got == (True, ""), repr(got))
    check("...and is recorded as unparseable",
          any(m.get("parse") == "none" for m in metrics), repr(metrics))

    metrics.clear()
    got = verdict("The user wants me to identify the dominant color of the provided image.")
    check("pure preamble (measured live) is open-and-recorded",
          got == (True, "") and any(m.get("parse") == "none" for m in metrics), repr(metrics))

    metrics.clear()
    check("an empty reply is open-and-recorded", verdict("") == (True, ""))
    check("...recorded too", any(m.get("parse") == "none" for m in metrics))

    print("--- shot plans survive prose, fences and stray brackets ---")
    ej = mod._extract_json
    check("bare array", ej('["a", "b", "c"]') == ["a", "b", "c"])
    check("fenced array", ej('```json\n["a", "b"]\n```') == ["a", "b"])
    check("array after prose", ej('Here are the shots:\n["a", "b"]') == ["a", "b"])
    # The greedy-regex killer: re.search(r"\[.*\]", ..., re.S) spans to the LAST bracket.
    check("prose with a trailing ] does not swallow the array",
          ej('Here: ["a", "b"]\nNote: see [1] for details.') == ["a", "b"],
          repr(ej('Here: ["a", "b"]\nNote: see [1] for details.')))
    check("a bracket inside a string is not a delimiter",
          ej('["a shot with [brackets] in it", "b"]') == ["a shot with [brackets] in it", "b"])
    check("truncated array -> None", ej('["a", "b"') is None)
    check("no json at all -> None", ej("I could not plan this.") is None)
    check("empty input -> None", ej("") is None)
    check("an object wrapping the array is still reachable",
          ej('{"shots": ["a", "b"]}') == {"shots": ["a", "b"]})

    fails = results.count(False)
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())
