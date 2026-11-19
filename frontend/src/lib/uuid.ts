const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Route and query ids are checked before they reach the API: anything else is "not found" without a request. */
export const isUuid = (value: string | null | undefined): value is string => typeof value === "string" && UUID.test(value);
