# Behaviours to tell the client

- Non-inventory BOM components (services, other charges) are excluded from the BOM (A12, to be confirmed with the client).
- A sub-assembly without a BOM of its own appears in StockTrim as a purchased item.
- Negative stock (A13): sent as-is. StockTrim stores negative stock on hand unchanged (verified on the TEST account: probe and the first real push read-back); the 134 negative items appear in StockTrim with their negative quantity. If a future StockTrim version rejects negatives, the engine retries those items with 0 and reports their ListIDs.
- Supplier city and phone cannot be transferred (the StockTrim supplier has no such fields): 333 of 382 suppliers have a city and 289 a phone (6 Nov pull).
- A value cleared in QuickBooks can never be cleared in StockTrim: omitted fields are kept and nulls behave like omission.
- Categories come from the top-level QuickBooks group. A category set in QuickBooks can never be cleared, and it will overwrite categories curated in StockTrim: needs a client decision before production.
- Inactive records (suppliers, customers) are not pushed.
- Cost, price and stock on hand are stored by StockTrim with 2 decimals, rounded half-up (1.005 becomes 1.01); we round the same way before sending, so a QuickBooks value with more decimals (e.g. a 3-decimal cost) will show rounded in StockTrim. For negative stock the tie rule (-1.005 -> -1.01) is assumed, not verified.
