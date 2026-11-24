"""BUILD IC-3 (Phase 8; §7 R3) — the usage probe: does this machine's anthropic SDK keep the thinking count of a streamed reply?
Our own few lines on Perplexity's idea (outputs/ic3/research/pplx_report.md, "Exact SDK 0.89.0 findings"): build ONE message_delta event
whose usage holds output_tokens_details.thinking_tokens, read it raw, then pass it through the SDK's own stream accumulator onto a
message snapshot. No client is made, no key is read, no call is sent. Prints two words: what the raw event keeps and what the
accumulator keeps — "raw:kept accumulator:dropped" is what the workspace's copy of Shane's environment printed on 11/11 (SDK 0.89.0),
and why the Oracle's call helper logs the raw event's usage (D5).
Usage: python -I -B sdk_usage_probe.py"""
import anthropic
from anthropic.lib.streaming._messages import accumulate_event
from anthropic.types import Message, RawMessageDeltaEvent

delta = RawMessageDeltaEvent.model_validate({
    "type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
    "usage": {"output_tokens": 1200, "output_tokens_details": {"thinking_tokens": 900}}})
raw = delta.usage.model_dump()
snap = Message.model_validate({"id": "msg_probe", "type": "message", "role": "assistant", "model": "probe", "content": [],
                               "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 0}})
acc = accumulate_event(event=delta, current_snapshot=snap).usage.model_dump()


def kept(u):
    d = u.get("output_tokens_details") if isinstance(u, dict) else None
    return isinstance(d, dict) and d.get("thinking_tokens") == 900


print(f"raw:{'kept' if kept(raw) else 'dropped'} accumulator:{'kept' if kept(acc) else 'dropped'}  (anthropic {anthropic.__version__})")
