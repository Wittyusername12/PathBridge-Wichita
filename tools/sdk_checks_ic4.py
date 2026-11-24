"""BUILD IC-4 (Phase 8; §7 R3) — Phase 0's three offline checks of this machine's anthropic SDK, one word each. No client is made, no key is
read and no call is sent: each check runs the SDK's own code on a value built here.
  (a) format: output_config with a json_schema format goes through the SDK's own request transform (the step that builds a body from the
      parameters of messages.stream and messages.create) unchanged — "unchanged", else "changed";
  (b) refusal: a refusal's stop_details.category "reasoning_extraction" (outside the SDK's Literal["cyber", "bio"]) is kept where the
      call helper reads it — a parsed message (a create) and the stream's raw message_delta event — "kept", else "dropped" (the stream's
      own accumulator drops it onto its snapshot, which is why the helper reads the raw event: D9, Phase 0);
  (c) batch: a batch result row's usage.output_tokens_details null, absent, zero and positive read apart (null and absent are not 0) —
      "apart", else "merged".
The workspace's copy of Shane's environment (SDK 0.89.0) printed "format:unchanged refusal:kept batch:apart" (evidence/sdk_checks_phase0.json
for the same three, there through a mock transport).
Usage: python -I -B sdk_checks_ic4.py"""
import anthropic
from anthropic._models import construct_type
from anthropic._utils import maybe_transform
from anthropic.types import Message, RawMessageDeltaEvent, message_create_params
from anthropic.types.messages import MessageBatchIndividualResponse

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["items"],
          "properties": {"items": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                             "required": ["preset", "set", "explanation"],
                                                             "properties": {"preset": {"type": "string"}, "set": {"type": "string"},
                                                                            "explanation": {"type": "string"}}}}}}
OC = {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}}
params = {"model": "claude-opus-5-5", "max_tokens": 100, "messages": [{"role": "user", "content": "x"}], "output_config": OC}
body_s = maybe_transform(dict(params, stream=True), message_create_params.MessageCreateParamsStreaming)
body_n = maybe_transform(dict(params, stream=False), message_create_params.MessageCreateParamsNonStreaming)
a = all(b.get("output_config") == OC for b in (body_s, body_n))

msg = {"id": "m", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": [], "stop_reason": "refusal",
       "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}
details = {"type": "refusal", "category": "reasoning_extraction", "explanation": None}
m = construct_type(type_=Message, value=dict(msg, stop_details=details))
delta = construct_type(type_=RawMessageDeltaEvent, value={"type": "message_delta", "usage": {"output_tokens": 7},
                                                          "delta": {"stop_reason": "refusal", "stop_sequence": None, "stop_details": details}})
def cat(x):
    d = getattr(x, "stop_details", None)
    if d is None and isinstance(getattr(x, "model_extra", None), dict):
        d = x.model_extra.get("stop_details")
    return d.get("category") if isinstance(d, dict) else getattr(d, "category", None)


b = cat(m) == "reasoning_extraction" and cat(delta.delta) == "reasoning_extraction"
seen = {}
for name, det in (("null", None), ("absent", "ABSENT"), ("zero", {"thinking_tokens": 0}), ("positive", {"thinking_tokens": 1234})):
    usage = {"input_tokens": 10, "output_tokens": 2000}
    if det != "ABSENT":
        usage["output_tokens_details"] = det
    r = construct_type(type_=MessageBatchIndividualResponse, value={"custom_id": f"r-{name}", "result": {"type": "succeeded", "message": dict(
        msg, stop_reason="end_turn", usage=usage)}})
    u = r.result.message.usage
    extra = getattr(u, "model_extra", None) or {}
    v = extra["output_tokens_details"] if "output_tokens_details" in extra else getattr(u, "output_tokens_details", "ABSENT")
    v = v.model_dump() if hasattr(v, "model_dump") else v
    seen[name] = "ABSENT" if ("output_tokens_details" not in extra and v in (None, "ABSENT") and det == "ABSENT") else v
c = (seen["null"] is None and seen["absent"] == "ABSENT" and (seen["zero"] or {}).get("thinking_tokens") == 0
     and (seen["positive"] or {}).get("thinking_tokens") == 1234)
print(f"format:{'unchanged' if a else 'changed'} refusal:{'kept' if b else 'dropped'} batch:{'apart' if c else 'merged'}"
      f"  (anthropic {anthropic.__version__})")
