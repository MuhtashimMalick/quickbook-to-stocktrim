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

## Open questions for the Phase 2 mapper
1. Units of measure: `UnitOfMeasureSetRef` is on most items; SO/PO lines can carry non-base units. Convert to the base unit or reject?
2. Customer emails containing `;` or `,` (9 cases): split, take the first, or drop?
3. Vendor address `Addr1` often holds the company name: skip it when it equals the vendor name?
4. Decimal serialisation: the Stage 5 DTOs must emit JSON numbers (not strings) for stockOnHand/cost/price/BOM quantity. Verify.
5. Negative stock (A13, 134 items): the rule for pushing negative `QuantityOnHand` (clamp to 0, push raw, or skip).
