-- Two token buckets checked and charged in ONE atomic step (docs/DECISIONS.md, D37-D38).
--
-- KEYS[1]  the API key's bucket       KEYS[2]  the tenant's shared bucket
-- ARGV[1]  key refill rate (tokens/s)   ARGV[2]  key capacity (max burst)
-- ARGV[3]  tenant refill rate           ARGV[4]  tenant capacity
--
-- A request costs one token from BOTH buckets, or from neither: a request refused by the
-- tenant limit doesn't drain the key's bucket, and vice versa.
--
-- Time comes from Redis (TIME), not from the caller, so gateway instances with slightly
-- different clocks still agree. Redis 7+ replicates a script's effects, not the script, so
-- calling TIME here is allowed and safe.
--
-- Returns {allowed (1/0), remaining (whole tokens, the smaller bucket), retry_after_ms,
--          limited_by (0 = none, 1 = key, 2 = tenant)}

local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000

local function refill(key, rate, capacity)
  local stored = redis.call('HMGET', key, 'tokens', 'ts')
  local tokens = tonumber(stored[1])
  local ts = tonumber(stored[2])
  if tokens == nil or ts == nil then
    return capacity -- first use, or the bucket expired because it was full
  end
  local elapsed = now - ts
  if elapsed < 0 then elapsed = 0 end
  return math.min(capacity, tokens + elapsed * rate)
end

local function save(key, tokens, rate, capacity)
  -- %.17g keeps full double precision; Lua's default tostring would round timestamps.
  redis.call('HSET', key, 'tokens', string.format('%.17g', tokens), 'ts', string.format('%.17g', now))
  -- Expire once the bucket would be full again: a full bucket and a missing one are the same.
  local ms_until_full = math.ceil((capacity - tokens) / rate * 1000)
  redis.call('PEXPIRE', key, ms_until_full + 1000)
end

local key_rate, key_capacity = tonumber(ARGV[1]), tonumber(ARGV[2])
local tenant_rate, tenant_capacity = tonumber(ARGV[3]), tonumber(ARGV[4])

local key_tokens = refill(KEYS[1], key_rate, key_capacity)
local tenant_tokens = refill(KEYS[2], tenant_rate, tenant_capacity)

local allowed, retry_after_ms, limited_by = 0, 0, 0
if key_tokens >= 1 and tenant_tokens >= 1 then
  allowed = 1
  key_tokens = key_tokens - 1
  tenant_tokens = tenant_tokens - 1
else
  -- Wait for whichever empty bucket takes longest to get back to one token.
  local key_wait, tenant_wait = 0, 0
  if key_tokens < 1 then key_wait = (1 - key_tokens) / key_rate end
  if tenant_tokens < 1 then tenant_wait = (1 - tenant_tokens) / tenant_rate end
  if tenant_wait > key_wait then
    limited_by = 2
    retry_after_ms = math.ceil(tenant_wait * 1000)
  else
    limited_by = 1
    retry_after_ms = math.ceil(key_wait * 1000)
  end
end

save(KEYS[1], key_tokens, key_rate, key_capacity)
save(KEYS[2], tenant_tokens, tenant_rate, tenant_capacity)

return {allowed, math.floor(math.min(key_tokens, tenant_tokens)), retry_after_ms, limited_by}
