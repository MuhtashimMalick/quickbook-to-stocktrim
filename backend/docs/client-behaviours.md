# Behaviours to tell the client

- Non-inventory BOM components (services, other charges) are excluded from the BOM (A12, to be confirmed with the client).
- A sub-assembly without a BOM of its own appears in StockTrim as a purchased item.
- Negative stock (A13): sent as-is first; the handling is filled in after the first real push.
- Supplier city and phone cannot be transferred (the StockTrim supplier has no such fields): 333 of 382 suppliers have a city and 289 a phone (6 Nov pull).
- A value cleared in QuickBooks can never be cleared in StockTrim: omitted fields are kept and nulls behave like omission.
- Categories come from the top-level QuickBooks group. A category set in QuickBooks can never be cleared, and it will overwrite categories curated in StockTrim: needs a client decision before production.
- Inactive records (suppliers, customers) are not pushed.
