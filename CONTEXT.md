# Amazon 商品采集上下文

本上下文区分采集是否完成、是否保存目标商品，以及商品身份解析结果，避免把商品族关系误报为采集故障。

## Language

**商品快照成功（Product Snapshot Success）**:
请求 ASIN 的身份得到确认，并保存了该请求商品的数据快照。
_Avoid_: 商品完成、普通成功

**同族变体解析（Variant Resolution）**:
请求 ASIN 被严格证据解析为同一明确 Parent 下的兄弟 Child；这是已完成的身份终态，但不保存兄弟商品为请求 ASIN 的快照。
_Avoid_: 变体失败、采集失败、商品成功

**采集失败（Collection Failure）**:
由于传输、解析或身份证据不足，无法形成可信商品快照或同族变体解析的终态。
_Avoid_: 商品问题、变体跳转

**访问阻断（Access Block）**:
Amazon 访问控制明确阻止本次采集，例如 CAPTCHA、WAF、403 或 429；它与商品身份问题及普通采集失败分开。
_Avoid_: 代理失败、商品失败

**已记录动作（Recorded Action）**:
一次请求 ASIN 已形成可审计终态；商品快照成功和同族变体解析都属于已记录动作。
_Avoid_: 商品成功数
