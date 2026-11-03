-- Renew existing keys without reading or rewriting the cached transcript.
-- The generation-scoped marker lets idle cleanup observe explicit interaction and
-- live worker activity atomically. Every key keeps a finite expiry after a crash.
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
local previous = tonumber(redis.call('GET', KEYS[3])) or 0
if tonumber(now) - previous < tonumber(ARGV[2]) then return 0 end
redis.call('SET', KEYS[3], now, 'EX', ARGV[1])
redis.call('EXPIRE', KEYS[1], ARGV[1])
redis.call('EXPIRE', KEYS[2], ARGV[1])
return 1
