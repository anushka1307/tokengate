-- Reserve tokens from a tenant's daily budget, atomically (docs/DECISIONS.md, D41-D44).
--
-- KEYS[1]  the tenant's counter for one UTC day (tokens used or reserved so far)
-- ARGV[1]  daily budget          ARGV[2]  estimated input tokens
-- ARGV[3]  requested max output   ARGV[4]  counter lifetime in seconds
--
-- The output limit is shrunk to fit what's left, so the model can't run past the budget.
-- Refused if not even one output token fits after the input estimate.
--
-- Returns {1, granted_output_tokens, remaining_after} or {0, remaining}.

local budget = tonumber(ARGV[1])
local input = tonumber(ARGV[2])
local max_output = tonumber(ARGV[3])

local used = tonumber(redis.call('GET', KEYS[1]) or '0')
local remaining = budget - used
local output = math.min(max_output, remaining - input)
if output < 1 then
  return {0, math.max(remaining, 0)}
end

redis.call('INCRBY', KEYS[1], input + output)
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[4]))
return {1, output, remaining - input - output}
