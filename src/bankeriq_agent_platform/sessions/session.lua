-- One document keeps the history view and provisional input changes atomic.
-- Progress uses its own bounded key and never parses this history document.
-- Cursors are decimal strings: Lua doubles cannot exactly compare all bigint values.
local function older(a, b)
    return #a < #b or (#a == #b and a < b)
end
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
local op = ARGV[1]
local incoming = cjson.decode(ARGV[2])
local raw = redis.call('GET', KEYS[1])
local state = nil
if raw then
    local ok, decoded = pcall(cjson.decode, raw)
    if ok and decoded.schema == '1' then state = decoded end
end
if op == 'cleanup' then
    local activity = tonumber(redis.call('GET', KEYS[2])) or 0
    if state and state.epoch == incoming.epoch and state.cursor == incoming.cursor
        and state.generation == incoming.generation
        and now - math.max(state.last_access or now, activity) >= incoming.idle_seconds then
        return redis.call('DEL', KEYS[1])
    end
    return 0
end
if state and older(incoming.generation, state.generation) then return false end
if not state or older(state.generation, incoming.generation) then
    state = {schema='1', generation=incoming.generation, cursor='0', view='',
             pending={}, deleted=false,
             last_access=now - math.max(0, incoming.last_activity_age)}
end
if state.deleted and op ~= 'tombstone' and op ~= 'peek' then return false end
if op == 'read' then
    return raw or false
elseif op == 'peek' then
    return raw or false
elseif op == 'publish' then
    if older(incoming.cursor, state.cursor) then return false end
    state.cursor = incoming.cursor
    state.view = incoming.view
elseif op == 'stage' then
    local previous = state.pending[incoming.request_key]
    if previous and cjson.decode(previous).fingerprint ~= incoming.fingerprint then return -2 end
    local count = 0
    for _ in pairs(state.pending) do count = count + 1 end
    if not previous and count >= 8 then return -3 end
    state.pending[incoming.request_key] = incoming.pending
elseif op == 'reconcile' then
    for _, key in ipairs(incoming.keys) do state.pending[key] = nil end
elseif op == 'tombstone' then
    state.deleted = true
    state.view = ''
    state.cursor = incoming.cursor
    state.pending = {}
else
    return redis.error_reply('unsupported session operation')
end
if op == 'stage' or incoming.touch then
    state.last_access = now
end
state.epoch = ARGV[3]
-- Republishing a snapshot or reconciling a draft is passive. Keep the deadline
-- anchored to interaction, live work or the latest committed conversation event.
-- An idle read may reconstruct a view for its caller without recaching it.
local activity = tonumber(redis.call('GET', KEYS[2])) or 0
local last = math.max(state.last_access, activity, now - incoming.last_activity_age)
local remaining = math.ceil((tonumber(ARGV[4]) - math.max(0, now - last)) * 1000)
if remaining > 0 then
    redis.call('SET', KEYS[1], cjson.encode(state), 'PX', remaining)
end
return cjson.encode(state)
