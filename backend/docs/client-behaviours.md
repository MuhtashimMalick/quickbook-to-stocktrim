# Behaviours to tell the client

- Non-inventory BOM components (services, other charges) are excluded from the BOM (A12, to be confirmed with the client).
- A sub-assembly without a BOM of its own appears in StockTrim as a purchased item.
- Negative stock (A13): sent as-is. StockTrim stores negative stock on hand unchanged (verified on the TEST account: probe and the first real push read-back); the 134 negative items appear in StockTrim with their negative quantity. If a future StockTrim version rejects negatives, the engine retries those items with 0 and reports their ListIDs.
- Supplier city and phone cannot be transferred (the StockTrim supplier has no such fields): 333 of 382 suppliers have a city and 289 a phone (6 Nov pull).
- A product value cleared in QuickBooks can never be cleared in StockTrim: omitted fields are kept and nulls behave like omission. Customers are the opposite: a customer PUT replaces the whole record, so a cleared email/phone/address IS cleared in StockTrim.
- Categories come from the top-level QuickBooks group. A category set in QuickBooks can never be cleared, and it will overwrite categories curated in StockTrim: needs a client decision before production.
- Inactive records (suppliers, customers) are not pushed.
- BOM quantities are stored by StockTrim with 5 decimals, rounded half-up (0.123456 becomes 0.12346); we round the same way before sending (summing repeated components first), so a QuickBooks quantity with more than 5 decimals shows rounded, and one below 0.000005 (which StockTrim would store as 0) is not sent and is reported as a warning.
- Customers cannot be deleted through the StockTrim API: every customer we push (13,588 on the first full load, plus the probe customer `zz-probe-cust`) can only be removed in the StockTrim UI. Customer codes are the QuickBooks ListIDs, matched case-insensitively.
- Cost, price and stock on hand are stored by StockTrim with 2 decimals, rounded half-up (1.005 becomes 1.01); we round the same way before sending, so a QuickBooks value with more decimals (e.g. a 3-decimal cost) will show rounded in StockTrim. For negative stock the tie rule (-1.005 -> -1.01) is assumed, not verified.
