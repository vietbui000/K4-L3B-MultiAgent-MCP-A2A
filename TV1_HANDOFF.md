# Cập nhật adapter SDK và input thật — 2026-09-25

- SDK MCP hiện tại dùng `input_schema`, `next_cursor`, `is_error`,
  `structured_content`. Gateway ưu tiên các field này, hỗ trợ alias camelCase
  cho object SDK cũ; test dùng model thật của SDK để tránh mock che lỗi.
- Entity đọc thêm `customer_request.claimed_order_id` và
  `customer_unique_id_hint`. Customer history thật trả `orders`; agent nối
  order_id/customer_id với lịch sử có customer_unique_id đã xác minh, không
  thay thế customer_unique_id bằng customer_id.
- Claimed ID chỉ được chọn sau kiểm chứng order/customer; nhiều candidate được
  xác nhận vẫn ambiguous. Tool lỗi chung không làm candidate trở thành rejected.
- Discovery thật đọc được 10 tool. Entity case `L3B_CASE_001` đã resolved qua
  MCP thật với 3 calls, 2 evidence được consume. Đây là kiểm tra Entity riêng,
  chưa phải chạy toàn bộ workflow hoặc tạo submission.
- Ruff pass; pytest **52 passed, 1 failed**: test repo phát hành không cho input
  thất bại vì workspace hiện đã có case-set và 100 input hợp lệ. Không xóa input
  hoặc thay test để che điều kiện này.
- Đã sửa Order Agent gọi `get_product_context` và `get_sellers` bằng `order_id`,
  mỗi tool một lần cho mỗi đơn đã resolve, không phụ thuộc ID trong items.
  MCP thật case 001 xác nhận cả hai call thành công và trả danh sách; parser
  đã nhận danh sách product/seller. Test dùng schema order_id và response list,
  kiểm tra nhiều đơn, scope trùng và tắt product context. Bộ test order/regression:
  26 passed, Ruff pass. Chưa chạy toàn bộ workflow 100 case.

Các ghi chú thiếu input/shape phẳng phía dưới là lịch sử bàn giao trước cập nhật này.

---

# Cập nhật sau review — 2026-09-25

Hiện đã có đủ bốn specialist. Hướng dẫn ban đầu bên dưới mô tả trạng thái lúc TV1
bàn giao; ghi chú thiếu module không còn áp dụng.

- Đã sửa tính trùng capture/refund giữa các nguồn, giữ số tiền chưa biết khi
  thiếu amount/identity hoặc tool lỗi; chặn hoàn tiếp khi refund pending/failed.
- Shipment giữ deadline theo order, không quy lỗi logistics khi thiếu bằng chứng
  seller; nhiều shipment chưa ánh xạ được seller sẽ giữ insufficient_evidence.
- Policy giữ conflict từ mọi specialist, chưa chọn nguồn khi thiếu quy tắc đã
  xác minh; lost/returned yêu cầu điều tra. Chỉ xác nhận split khi nhiều capture
  khớp `total_brl` của một order trong adapter hiện tại.
- Verifier gọi business checks, kiểm tra tiền/refund trùng/conflict. Case không
  có identifier có thể xuất not_found với refs rỗng đúng schema; resolved vẫn
  bắt buộc evidence. Pass schema không đảm bảo điểm evidence.
- Sửa test thiếu module bằng mock; thêm regression test và tích hợp specialist
  thật với gateway giả. **Ruff pass; toàn bộ pytest: 44 passed.** Trên máy này
  pytest cần quyền thư mục tạm ngoài sandbox để tránh WinError 5.

**Chưa kiểm chứng MCP thật:** workspace thiếu `case-set.json`; validate-inputs
đã báo FileNotFoundError rõ ràng. Chưa chạy 100 case, validate output thật/package.
Adapter field cần đối chiếu response thật. Policy hiện hiểu `refund_full` boolean
cho một order, chưa tự phân bổ refund nhiều order hoặc đoán source precedence.
Lịch sử trạng thái chưa đủ xác định trạng thái cuối được xử lý bảo thủ. Entity
vẫn hỗ trợ shape phẳng, chưa ranking theo amount/time/item.

---

# TV1 — Hướng dẫn tích hợp ban đầu

## Đã triển khai

- `a2a.py`: Task, Result, Specialist và kiểm tra correlation/ref consumption.
- `evidence_collector.py`: discovery arguments validation, allowlist, cache và
  gộp request trùng theo case, registry, consumption trace, timeout 30 giây,
  tối đa 1 retry transport/timeout, budget 20 calls gồm retry.
- `agents/entity.py`: resolve có kiểm chứng order evidence; candidate cần
  customer discriminator, không chọn chỉ vì ID tồn tại. Reject khi customer
  mâu thuẫn; missing identifier/response shape lạ dừng để sửa adapter.
- `workflow.py`: entity → 3 specialist đồng thời → policy → verifier;
  deadline task 120 giây, case 600 giây, hủy/chờ task con khi lỗi.
- `agents/verifier.py`: schema, finite JSON, case/scope, evidence consumed,
  claim refs, refund totals/bounds và conflict selected_source.

Schema công khai giữ nguyên. CLI vẫn sở hữu case_received/case_finalized.
Không xuất kết quả giả khi chưa có specialist.

## Giao diện TV2/TV3 cần triển khai

TV2 tạo `agents/order.py`, `agents/shipment.py`. TV3 tạo `agents/payment.py`,
`agents/policy.py`. Mỗi module export:

```python
async def run(task: Task, collector: EvidenceCollector) -> Result:
    ...
```

Task chứa case_id, recipient, task_type, entity_scope, payload và message_id.
Result echo case_id/message_id; sender phải bằng task.recipient. `status`
phải là completed, error_code=None để coordinator nhận kết quả thành công.
Result.payload là dict; evidence_refs chỉ gồm refs actor đó thực sự consume.

Lấy evidence bằng `await collector.call(task.recipient, tool_name, **arguments)`;
không truyền case_id vì collector tự gắn. Chỉ dùng argument có trong tool
inputSchema tại collector.tools. Sau khi dùng dữ liệu, gọi
`collector.consume(task.recipient, [evidence_ref])`. Không mutate registry.

Specialist nhận payload `{case, entity}` và entity_scope đã resolve. Policy
nhận `{case, entity, entity_evidence_refs, findings}`; findings map tên order,
payment, shipment tới `{payload, evidence_refs}`. Khi entity chưa resolve,
findings rỗng và policy phải giữ needs_investigation.

Policy trả payload là draft output L3B; coordinator gắn schema_version/case_id
và entity context đã kiểm chứng. Draft.evidence_refs chứa refs thực sự hỗ trợ
kết luận, bao gồm refs specialist được bàn giao. Riêng Result.evidence_refs
của policy chỉ liệt kê refs policy tự consume qua tool được phép của mình.
Không consume lại tool của actor khác để vượt allowlist.

TV3 cần bổ sung kiểm chứng nghiệp vụ chi tiết trong business_checks.py và
tích hợp cùng TV1: source precedence, timeline, item/seller/payment/shipment
ownership, claim support và confidence calibration. Verifier hiện chỉ kiểm
tra scope order và các invariant chung; schema không chứng minh nghiệp vụ đúng.

## Giới hạn cần xác nhận với dữ liệu thật

Repo hiện không có input L3B; chưa thể kiểm chứng adapter với gateway thật.
Adapter entity hiện nhận các field phẳng: order_id/order_ids,
candidate_order_ids, customer_unique_id (không thay customer_id cho unique ID).
Order evidence cần data.order_id và data.customer_unique_id khi có customer
constraint. History cần data.customer_unique_id; related_order_ids nếu có là
list string. Không có related_order_ids thì context hiện trả list rỗng, chưa
khẳng định đã khai thác hết lịch sử khách hàng. Schema data của MCP là mở,
nên các shape này là adapter giới hạn, không phải contract công khai của server.

Chưa hỗ trợ candidate ranking theo amount/time/item hoặc input lồng nhau.
Không tự mở rộng parser bằng cách đoán field. Cần input thật và response đã
loại secrets để hoàn thiện adapter. Entity confidence 0.9 là mức tạm cho
identifier khớp, chưa phải xác suất đã calibration. Với exact order thiếu
customer_unique_id, chưa tự truy ngược customer history.

Thiết kế ban đầu cho phép một lượt sửa draft; implementation hiện fail-fast
ở lỗi verifier và không có vòng sửa tự động. Cache/registry nằm trong process,
không chứng thực team/run ownership phía server; audit MCP là nguồn cuối.

## Kiểm tra và bàn giao

```text
python -m ruff check .
python -m pytest -q -p no:cacheprovider tests/test_workflow_tv1.py tests/test_contract_lock.py
```

Tests dùng MCP/agent fixtures, không phải chạy nghiệp vụ thật. Chỉ gọi
day09 run sau khi có bốn specialist và input thật. `solve_case` báo lỗi rõ
khi thiếu module trước khi gọi MCP. Chưa đạt mốc một case thật xuyên suốt;
chưa chạy 100 case, chưa package hoặc push lên GitHub.
