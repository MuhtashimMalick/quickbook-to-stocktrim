# Domain layer notes (`app/domain`)

Pure QuickBooks -> canonical logic; no StockTrim, no QBWC. No client data in this file.

## Keys
- Product SKU = QuickBooks item `Name` (`product_key_field`, `ProductKeyField` in `domain/models.py`). The pushed key is always the exact stored string.
- Collision comparison only: `unicodedata.normalize("NFKC", key.rstrip(" ")).casefold()`. So `Straße`/`STRASSE`, NFC/NFD `café`, full-width `Ａ`/`A`, `X`/`x` and `X`/`X ` collide. Trailing spaces are ignored (StockTrim `GET ?code=` ignores them); leading spaces/tabs are not.
- Vendor keys and item keys are separate namespaces (resolved separately). Inventory and assembly items share ONE namespace.

## Quarantine rules (`resolve`)
Quarantined (not pushed, reported with `Issue`): empty/blank key, leading/trailing whitespace, control characters, empty ListID (`empty_list_id`), every member of a key-collision group or a duplicate-ListID group (`duplicate_key`), records that failed to parse (`parse_error`).
Kept and only warned: key longer than 200, internal double space, non-ASCII.
`parse_error` messages carry the exception type and the failing FIELD name (pydantic: the error `loc`), never the value.

## BOM
- Non-inventory/unknown components are excluded and counted (A12 default: only inventory and assembly items are components); quarantined or unparseable products are reported as `bom_component_excluded` (as component or as parent). Pass `{i.ref for i in snapshot.issues}` as `unusable_list_ids`.
- Self-reference: `bom_self_reference`, severity warning; the line is dropped (`dropped_edges`), the parent stays pushable. Checked before quantity. Non-positive/non-finite quantity: dropped, counted in `dropped_edges`.
- Cycle policy: every cycle member and every ANCESTOR of a cycle member (reverse transitive closure over the full parent graph) is withheld (`bom_cycle` / `bom_cycle_ancestor`, both quarantine) and listed in `quarantined_parents`; `cycle_ancestors` counts the ancestors. Descendants below a cycle and unrelated assemblies stay pushable.
- Depth: `max_depth`/`depth_histogram` = longest chain of pushed BOMs; an assembly counts as a level only if it has at least one pushed edge.
- Summed duplicates: a component repeated in one BOM is summed into one edge. `summed_duplicates` counts LINES merged (dev: 18); `assemblies_with_summed_duplicates` counts assemblies with pushable duplicated components (dev: 12; 14 assemblies repeat a component of any type).
- `subassemblies_without_bom`: assemblies used as a component of a pushed edge that have no pushed BOM of their own (Phase 2 must push them as plain products without a BOM).

## Suppliers
Phase 2's supplier push must use exactly `resolve_suppliers(...).ok`. Inactive vendors (`IsActive=false`) are valid but go to `.inactive` with an `inactive_supplier` warning and are never pushed. A product whose preferred vendor is inactive, quarantined or unknown gets `unresolved_vendor` and `preferred_vendor_code=None` (set only by `resolve_products`).

## Phase 2 mapper decisions (`app/sync/mappers.py`)
1. Units of measure: still open (SO/PO mappers, later stages).
2. Several emails (`;` or `,`): the first valid one is sent, none if no part is valid. A leading `mailto:` and `Name <addr>` are accepted; validity is checked by `email-validator` without DNS. Internationalised addresses (`ä@x.de`) are accepted (returned NFC-normalised).
3. Addresses: Addr1..Addr5 lines equal (casefold, trimmed) to the vendor code/company name (customer: name) are dropped; the rest become streetAddress, addressLine1 and addressLine2 (3rd and later lines joined with ", "). SupplierDto has no city and no phone, so neither is sent for suppliers (known limitation: on the 6 Nov pull 333 of 382 suppliers have a city and 289 a phone; no mapping is invented).
4. Decimal serialisation: the DTOs use float fields, so payloads carry JSON numbers (tested on the serialised payload).
5. Negative stock (A13): `NEGATIVE_STOCK_POLICY` `as_is` (default, send the negative) or `clamp` (send 0); `to_product_request` returns `(request, stock_adjusted)`; `PushPlan` lists negative-stock ListIDs and (under `clamp`) adjusted ListIDs.
6. Product category: top-level group of `FullName` when it has a ':' parent, else omitted. Supplier link only when `preferred_vendor_code` is set, and it sends ONLY `supplierId` (+ `supplierSkuCode` when a manufacturer part number exists): `supplierName` is never sent (undocumented, it could rename the supplier created by the supplier push). An empty top-level group (`:Item`) gives no category. The product `name` has whitespace runs collapsed (keys never). `maximumShelfLevel` only when Max > 0. `stockOnOrder` never.
7. Inactive customers (`IsActive=false`) go to `resolve_customers(...).inactive` with an `inactive_customer` warning and are never pushed (the plan uses `.ok`).
8. BOM quantity precision (changed in stage 10 after a live probe): StockTrim stores BOM quantities with 5 decimals, ROUND_HALF_UP. The planner rounds each SUMMED edge quantity the same way (`BOM_QUANTITY_STEP = 0.00001`), so `bom_edge.quantity` (Numeric(20,6)), the fingerprints and the diff all use the value StockTrim stores; an edge that rounds to 0 is dropped (`bom_quantity_precision` warning, counted in `dropped_edges`).
9. `build_push_plan` sorts suppliers, products and customers by key and raises `ValueError` when its inputs disagree (BOM edge end not a planned product; product vendor not a planned supplier).
10. BOM edge key: `bom_edge_key(parent, component)` = `json.dumps([parent, component], ensure_ascii=False)`; used by the plan and by `push_item.key` (the engine must use it too), so a SKU containing `|` cannot collide.
11. `bom_edge` and `push_run` carry a `target` (`stocktrim_target(settings)`: environment | normalised base URL | sha256(auth id)[:12]); the BOM edge key is unique per (target, parent, component).
