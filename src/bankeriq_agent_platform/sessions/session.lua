-- One bounded document keeps view, provisional input and progress changes atomic.
-- Cursors are decimal strings: Lua doubles cannot exactly compare all bigint values.
local function older(a, b)
    return #a < #b or (#a == #b and a < b)
end
local now = tonumber(redis.call('TIME')[1])
local op = ARGV[1]
local incoming = cjson.decode(ARGV[2])
local raw = redis.call('GET', KEYS[1])
local state = nil
if raw then
    local ok, decoded = pcall(cjson.decode, raw)
    if ok and decoded.schema == '1' then state = decoded end
end
if op == 'cleanup' then
    if state and state.epoch == incoming.epoch and state.cursor == incoming.cursor
        and state.generation == incoming.generation
        and now - (state.last_access or now) >= incoming.idle_seconds then
        return redis.call('DEL', KEYS[1])
    end
    return 0
end
if state and older(incoming.generation, state.generation) then return false end
if not state or older(state.generation, incoming.generation) then
    state = {schema='1', generation=incoming.generation, cursor='0', view='',
             pending={}, progress={}, progress_sequence='0', deleted=false,
             last_access=now - math.max(0, incoming.last_activity_age)}
end
if state.deleted and op ~= 'tombstone' and op ~= 'peek' then return false end
if op == 'read' then
    if not raw or state.view == '' then return false end
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
elseif op == 'progress' then
    -- A later run/attempt starts a new disposable sequence. Late producers from
    -- an older attempt cannot replace the current run's display after reconnect.
    local generation = state.progress_generation or '0'
    local attempt = state.progress_attempt or '0'
    if older(incoming.progress_generation, generation) then return false end
    if incoming.progress_generation == generation and older(incoming.attempt, attempt) then
        return false
    end
    if generation ~= incoming.progress_generation or attempt ~= incoming.attempt then
        state.progress = {}
        state.progress_sequence = '0'
    end
    state.progress_generation = incoming.progress_generation
    state.progress_attempt = incoming.attempt
    if not older(state.progress_sequence, incoming.sequence) then return false end
    table.insert(state.progress, incoming.progress)
    if #state.progress > 32 then table.remove(state.progress, 1) end
    state.progress_sequence = incoming.sequence
elseif op == 'tombstone' then
    state.deleted = true
    state.view = ''
    state.cursor = incoming.cursor
    state.pending = {}
    state.progress = {}
else
    return redis.error_reply('unsupported session operation')
end
if op == 'read' or op == 'stage' or op == 'progress' or incoming.touch then
    state.last_access = now
end
state.epoch = ARGV[3]
redis.call('SET', KEYS[1], cjson.encode(state), 'EX', ARGV[4])
return cjson.encode(state)
