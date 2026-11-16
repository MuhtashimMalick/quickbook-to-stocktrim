# StockTrim API: observed behaviour (live probe, 2026-11-16)

Probed through `app/stocktrim/client.py` against the **TEST** account, with synthetic records named `ZZ-PROBE-*`. Every record created was deleted and verified gone (see end). Bodies are trimmed to their shape. No client data, no credentials.
Swagger: `https://api.stocktrim.com/swagger/v1/swagger.json` (info.version `v1`). Where the API contradicts it, this file wins.

## 0. The test account is NOT empty
Read-only counts: **319 products** (7 pages), **1173 customer rows** (20 rows belong to 9 groups of exactly duplicated customer codes, so **1162 distinct codes**; some rows are placeholders with the literal text `string`), 0 purchase orders, no sales orders readable. Product `productId` and `productCodeReadable` are different on the existing products (e.g. numeric id vs a readable code). So "StockTrim is empty" does not hold for this account; a real production pre-flight (T1.3) must be written against that fact. Existing records were never modified.

## 1. General
| Topic | Observed |
|---|---|
| Success codes | POST/PUT/DELETE answer **200** (swagger says 201 for POST) or **204** (PurchaseOrders DELETE). Treat any 2xx as success. |
| Errors | 400 `{"errors":{"Field":["msg"]},"title":"One or more validation errors occurred.","status":400}` (validation); 404 either bare `{"title":"Not Found","status":404,"traceId":..}` or `{"title":"Could not find a ... ","status":404}`. 429 and Retry-After were never seen. **5xx does occur: see "Authentication failures" below** (an earlier version of this note said "no 5xx seen"; that was wrong). |
| Authentication failures | An **unknown `api-auth-id`**, or **empty-string** id/signature headers -> **500 with an EMPTY body**. A **real id with a wrong signature** -> **401, body not JSON** (plain text). **No auth headers at all** -> **401**. So a 500 can mean "wrong STOCKTRIM_AUTH_ID", and a client that retries 5xx will retry a configuration error (the client's `StockTrimUnavailable` message says so; 401/403 are `StockTrimAuthError`, never retried). |
| Unknown JSON fields in a request | **Silently ignored on POST /api/Products only** (`bogus` field -> 200), despite swagger `additionalProperties:false`. **Not tested on any other endpoint.** Our request models still forbid them. |
| Explicit `null` in a request | Tested on **POST /api/Products** with `name`, `price`, `stockOnHand` and `productCodeReadable` all `null` on a stored record (re-test 2026-11-16): `name` and `price` **kept**, `stockOnHand` **cleared** and `productCodeReadable` **reset to productId**. So `null` behaves like omission (section 2), not like "keep the stored value" for those two fields. Other endpoints not tested. |
| Ids are case-insensitive | `POST productId=zz-probe-1` updated the record created as `ZZ-PROBE-1` (same `id`) and rewrote the stored id to lowercase. `GET /api/Products?code=` and `DELETE ?productId=` are case-insensitive too. BOM POST with lowercase ids hit the same edge. |
| Whitespace and symbols | Double space (`ZZ-PROBE-4  X`), trailing space (`ZZ-PROBE-5 `), `U+2122` and `U+0099` in a productId are all **stored and returned exactly** (POST echo and list scan). `GET ?code=` finds the exact string; `ZZ-PROBE-4 X` (single space) -> 404; but `code=ZZ-PROBE-5` (no trailing space) **does** find `ZZ-PROBE-5 `. Rule established read-only on two existing products whose readable code differs from productId: the exact code, code + 1 space and code + 2 spaces all return 200 with an exact match; a LEADING space, spaces on both sides, or a trailing TAB return 404. So **trailing spaces are ignored on both the query and the stored value, leading whitespace and tabs are significant**. The earlier "padded" probe used `" ZZ-PROBE-A-R "` (spaces on both sides), so its 404 came from the leading space: no contradiction. Consequence: codes that differ only by trailing spaces ("X" vs "X ") cannot be told apart in lookups, so T1.2/T1.3 must detect that collision. Whether `/api/boms` follows the same rule was NOT tested. Suppliers: `ZZ-PROBE-SUP2 ` (trailing space) stored and found only with the space. |

## 2. Products (`/api/Products`)
- **GET no filter**: 200, array of 50 (page size fixed 50). `pageNo` 1 and 0 and absent: same first page; `pageNo=2` next 50; `pageNo=999` -> 200 `[]`. Walk pages until `[]`.
- **GET `?code=`**: matches **`productCodeReadable`**, **not `productId`** (re-confirmed 2026-11-16 with ZZ-PROBE-A: `code=ZZ-PROBE-A-R` and `code=zz-probe-a-r` -> 200 with that one product; `code=ZZ-PROBE-A` (the productId), the numeric id, and a query with a leading space -> 404; see the whitespace rule in section 1). Callers must still compare the returned code exactly. `code=<productId of an existing product whose readable code differs>` -> **404**; `code=<numeric internal id>` -> 404. A non-matching code -> **404** (not `[]`; body bare Not Found). The client treats a 404 read as empty.
- **POST** (V1, one product per call): 200, body = the full stored product incl. internal numeric `id` and defaults (`serviceLevel 0.95`, `discontinued false`, `unstocked false`). `productCodeReadable` defaults to `productId` when omitted. Missing `productId` -> 400 `{"errors":{"ProductId":["The ProductId field is required."]}}`; empty string -> same 400; a JSON array body -> 400.
- **Upsert by productId (case-insensitive)**: re-POST of the same id -> 200 and the same `id`; no duplicate, no error.
- **Omitted fields on update** (full record then a re-POST of only `productId` + `price`): kept = name, category, subCategory, leadTime, stockOnOrder, cost, supplierCode, barcode, discontinued, min/max shelf, weight, option1; **cleared / reset = `stockOnHand` (7.0 -> null) and `productCodeReadable` (-> equals productId)**; changed = price. Consequence: always send `stockOnHand` and `productCodeReadable` on every product POST; you cannot clear another field by omitting it.
- **DELETE `?productId=`**: 200 empty body; unknown id -> 404 `{"title":"No product could be found with the provided code"}`. Verified by GET -> 404.

## 3. Suppliers (`/api/Suppliers`)
- **POST** takes an **array** (an object body -> 400 "Cannot deserialize the current JSON object ... List<SupplierRequestDto>"); `[]` -> 200 `[]`; element without `supplierCode` -> 400 `[0].SupplierCode required`. Response 200 = array of the stored suppliers (with `id`).
- **Upsert by supplierCode**: re-POST with a changed name and no email -> 200, name changed, **email kept** (omitted = kept).
- **GET `?code=`** returns a **single object** (not an array; swagger agrees). Unknown -> 404 `{"title":"Could not find a supplier with code X"}`. **GET without code -> 404** (`"...with code "`): `GET /api/Suppliers` itself has no list-all, but see `SuppliersBulk` below.
- **`GET /api/SuppliersBulk`** (probed read-only 2026-11-16, TEST account): 200 and a JSON **array of all suppliers in one response** (6 on the TEST account). Fields per element: `id, supplierCode, supplierName, emailAddress, externalId, defaultLeadTime, primaryContactName, streetAddress, addressLine1, addressLine2, state, country, postCode`. **No paging**: `page`, `pageNo`, `pageSize` (and `code`) are ignored (same 6 rows for page 1, 2 and 999; pageSize=2 still returns all 6). The empty-account answer (404 vs `[]`) could not be observed on this non-empty account; `StockTrimApi.list_suppliers_bulk()` accepts both. This is the pre-flight's supplier inventory; the per-key `?code=` probe is only an opt-in cross-check.
- **DELETE `?supplierCodeOrName=`**: 200 empty; unknown -> 404 `Could not find a supplier with code or name X`. Verified by GET -> 404.
- **Side effect**: a purchase order that names an unknown supplier code **auto-creates that supplier** (see 7). Delete it afterwards.

## 4. Bill of materials (`/api/boms`)  (multi-level add-on: NOT blocked on this account)
- **KEY FINDING: `/api/boms` is keyed by `productCodeReadable`, NOT by `productId`** (write probe 2026-11-16 on the TEST account: products `productId=ZZ-PROBE-A` / `productCodeReadable=ZZ-PROBE-A-R`, same for B; V1 POST product accepts differing values and stores them as sent; internal ids 13150143 / 13150144):
  | Call | Key used | Result |
  |---|---|---|
  | POST /api/boms | productId (`ZZ-PROBE-A` -> `ZZ-PROBE-B`) | **400** `Could not find a Sku Product with Id ZZ-PROBE-A` |
  | POST /api/boms | productCodeReadable (`ZZ-PROBE-A-R` -> `ZZ-PROBE-B-R`) | **200**, edge created (echo repeats the readable codes) |
  | POST /api/boms | numeric internal ids | **400** `Could not find a Sku Product with Id 13150143` |
  | POST /api/boms | mixed (productId parent, readable component) | **400** (parent not found) |
  | GET both keys | readable | **200** (`skuProduct`/`skuComponent` hold the full products; the BOM keys are in `skuProduct.productCodeReadable` / `skuComponent.productCodeReadable`; in this very first probe productId and readable happened to be equal) |
  | GET both keys | productId / numeric / mixed | **404** `Could not find a Sku Product with Id ...` |
  | DELETE both keys | readable | **200** (edge gone); the other three forms -> 404 |

  Consequence: a sync must store and send each product's **`productCodeReadable`** as the BOM key (and keep it unique and stable), never its `productId`. `BomResponse.product_code/component_code` return the readable code. This also means that the BOM key and the product upsert key (productId) differ; always POST products with an explicit `productCodeReadable`.
- Reminder from the read-only probe: for the existing products, GET by productId returns 404 while the productCodeReadable resolves (same cause).
- **POST** -> 200, body `{id, assemblyTimeDays:null, skuComponent:null, skuProduct:null, productId, componentId, quantity}` with the codes as sent. TOP->SUB x2, SUB->RAW1 x3, SUB->RAW2 x1.5, TOP->RAW1 x0.25 all accepted, so a **multi-level BOM (TOP->SUB->RAW1) is accepted**; the account's multi-level capability was not otherwise verified in the UI.
- **GET: returns 404 unless BOTH `productId` and `componentId` are given** (no filters, productId only, componentId only: bare 404 even though the rows exist; this is the known bug). With both ids: 200 single object whose **`productId`/`componentId` hold StockTrim's numeric product ids** (e.g. `"13150135"`) and the products are in `skuProduct` / `skuComponent`; the BOM keys are their `productCodeReadable` (the nested `productCodeReadable` was NOT recorded in a captured GET body, it is assumed from swagger, whose ProductsResponseDto has the field: listed under "Could not determine"; the client falls back to the nested `productId` when it is absent). A pair that is not linked -> bare 404; unknown product -> 404 `Could not find a Sku Product with Id X`; unknown component -> 404 `Could not find a Sku Component with Id X`. So **edges cannot be listed from StockTrim**: the sync must diff against its own stored edge set and probe single pairs.
- **Upsert by pair**: duplicate POST of the same pair and quantity -> 200 same `id`; same pair with a new quantity (2 -> 5) -> 200 same `id`, quantity **updated** (GET confirms). No duplicate rows. Ids are case-insensitive.
- **Not validated**: self-reference (RAW2->RAW2) accepted; a **cycle** (TOP->SUB then SUB->TOP) accepted; quantity `0` accepted (stored 0.0), `null` accepted (stored null), `-1` accepted. The sync must reject self-references, cycles and non-positive quantities itself.
- Unknown product or component on POST -> **400** `{"title":"Could not find a Sku Product|Component with Id X"}`. Missing `componentId` -> 400 validation.
- **DELETE with both ids**: 200 empty; the pair is gone (GET -> 404), other edges of the same parent/component untouched. Repeat delete of a gone pair -> 404 bare; unknown component -> 404 `Could not find a Sku Component`.
- **Not tested on purpose: DELETE with only productId or only componentId**, and DELETE with no params. The probe runner refused that action because this account holds data; the scoped test would only have hit `ZZ-PROBE-*` ids but was not retried. Treat a one-id delete as "may remove several edges"; the client refuses it.

## 5. Sales orders (`/api/SalesOrders`)
- **POST** -> 200, body `{id, locationId:0, productId, externalReferenceId, orderDate:"...Z", quantity, unitPrice, ...}`. Missing `orderDate` -> 400.
- **Upsert by `externalReferenceId`** (re-POST with the same reference): same `id`, no duplicate; changed quantity/price/date **updated** (GET shows 7 / 12 / 2026-11-02). A different reference -> new row. **No reference: every POST creates a new row** (two identical posts -> two rows). So always send a stable `externalReferenceId`.
- **Unknown productId is accepted** (a row is stored for a product that does not exist; no product is created).
- **GET `?productId=`** -> 200 array (orderDate without `Z`); none -> 404 (also GET without productId -> 404). **DELETE `?productId=`** -> 200 empty (removes all rows of that product), then GET -> 404; DELETE of a product with no rows -> 404.

## 6. Customers (`/api/Customers`)
- GET -> 200, one array of all customers (270 KB for 1173; no paging). `GET /api/Customers/{code}` unknown -> bare 404. **PUT was not probed**: swagger has no customer DELETE, so a test record could not be cleaned up.

## 7. Purchase orders
- **V2 list `GET /api/V2/PurchaseOrders`**: 200 array; `[]` when none (not 404). `status=Draft|Approved|Sent|Received` accepted; `status=1|2|3` accepted; **`status=0` -> 400** `{"errors":{"status":["The value '0' is invalid."]}}`. `page=0|1`, `pageSize=5` accepted (paging beyond a one-row result was not exercised).
- **`GET /api/PurchaseOrders?referenceNumber=`** (v1) -> 200 object but **`purchaseOrderLineItems: null`** and `status` as an **integer** (`0`); **`GET /api/V2/PurchaseOrders/{ref}`** and the V2 list return lines and `status` as a **string** (`"Draft"`). Observed an **integer status on the V2 list too** (`status: 0`) for a PO created without a status. Parse both (the client does). Use V2 for reading.
- **POST /api/PurchaseOrders** -> 200, echo with `id`, `message:""`, `status`. Required by us: supplier + lines. Unknown status string -> 400 `Error converting value "Bogus" to type PurchaseOrderStatusDto`.
- **Identity of POs created through the API is `clientReferenceNumber`, not `referenceNumber`**: re-POSTing with the same `clientReferenceNumber` and a **different `referenceNumber`** updated the same PO (same `id`) and **renamed its referenceNumber**. A POST without `referenceNumber` and with a new client reference created a second PO (`referenceNumber: null`). **Caveat: this rests on one rename test.** The identity of POs **created in the StockTrim UI** (which may have no `clientReferenceNumber`) is **UNKNOWN**: do not assume the sync can update them by client reference. Our DTO requires a `clientReferenceNumber` for every POST.
- **Lines are merged, not replaced**: re-POST with lines `[PO3 x9]` onto a PO with `[PO1 x5, PO2 x3]` gave `[PO1 x5, PO2 x3, PO3 x9]` on read (the echo showed only the posted line; the read shows the merge). A later POST changing PO1 quantity to 1 updated that line. **Lines can't be removed by re-POST.** Status changed Draft -> Approved on re-POST and the status filter followed.
- **`referenceNumber` query on GET and DELETE also matches `clientReferenceNumber`** (a PO with null referenceNumber was found and deleted via its client reference). DELETE -> **204**; the old referenceNumber after a rename -> 404.
- **Unknown supplier code and unknown product code are accepted** (200); the unknown supplier was **auto-created**; no product was created.

## 8. RunForecastCalculations
`POST /api/RunForecastCalculations` (no body) -> **200, empty body** (swagger says 201). The calculation outcome was not checked (nothing meaningful to forecast on the probe data).

## 9. Could not determine
- One-id and no-id DELETE of boms (runner refused; see 4).
- Whether unknown JSON fields or explicit nulls are treated the same on endpoints other than Products.
- Whether the nested `skuProduct.productCodeReadable` is really present in a live GET /api/boms body (assumed from swagger).
- Whether `/api/boms` ignores trailing spaces in its keys the way `/api/Products?code=` does.
- Identity of purchase orders created in the StockTrim UI (see 7).
- PUT /api/Customers response and behaviour (no cleanup route).
- Retry-After / 429 / 5xx behaviour of the real service (never triggered; covered offline only). Rate limits beyond the client's 5 requests/s were not exercised.
- Multi-level BOM effect in StockTrim's forecast/order plan (UI-only; needs demand history).
- `GET /api/V2/PurchaseOrders` paging with more than one page and whether its paging base is 0 or 1 (the pre-flight therefore also asks for `page=0` and for an unfiltered list, and takes the larger count: a PO created without a status comes back with integer status 0 that no status filter returns). `/api/SuppliersBulk` on an empty account (404 or `[]`).
- Whether `GET /api/SalesOrders?productId=` is case-sensitive (could not determine; products and BOM ids are case-insensitive, sales orders were not tested). StockTrim stores sales orders for products that do not exist, so an empty product list does not mean there are no sales orders.
- Whether stock/PO flags change `stockOnOrder` (not probed).

- DONE (stage 8): a process-wide rate limiter per StockTrim base URL (`app/stocktrim/limiter.py`); every `StockTrimClient` shares it unless a test injects `limiter=`. The key is the normalised base URL; `RateLimiter.defer(seconds)` pushes the next slot forward (used on 429 in the next stage).

## 10. Cleanup verification
Second probe (BOM key + null test, 2026-11-16): created only `ZZ-PROBE-A`, `ZZ-PROBE-B`, `ZZ-PROBE-N` (and the one A->B edge by readable key). Deleted the edge with both keys and the three products; GET by readable code and by productId -> 404 for all three, GET of every BOM key form -> 404.

First probe:
After the probe: products 319 (as before), no `ZZ-PROBE*` product (case-insensitive scan of all pages); suppliers `ZZ-PROBE-SUP`, `-SUP2 `, `-SUP3`, `-NOPE` (auto-created), `-SUPX` all 404; V2 purchase orders `[]`; sales orders for `ZZ-PROBE-SO`, `-NOPE`, `-PO1` 404; all BOM pairs among the four test products 404; customers 1173 with no `ZZ*` code (none were created).
