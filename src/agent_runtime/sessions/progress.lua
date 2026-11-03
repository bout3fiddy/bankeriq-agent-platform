-- Progress has its own bounded key: reading or updating it never parses history.
local function older(a, b)
    return #a < #b or (#a == #b and a < b)
end
local raw = redis.call('GET', KEYS[1])
if ARGV[1] == 'read' then return raw or false end
local incoming = cjson.decode(ARGV[2])
local state = raw and cjson.decode(raw) or
    {progress={}, sequence='0', run='0', attempt='0'}
if older(incoming.progress_generation, state.run) then return false end
if incoming.progress_generation == state.run and older(incoming.attempt, state.attempt) then
    return false
end
if incoming.progress_generation ~= state.run or incoming.attempt ~= state.attempt then
    state.progress = {}
    state.sequence = '0'
end
if not older(state.sequence, incoming.sequence) then return false end
state.run = incoming.progress_generation
state.attempt = incoming.attempt
state.sequence = incoming.sequence
state.progress[#state.progress + 1] = incoming.progress
if #state.progress > 32 then table.remove(state.progress, 1) end
local encoded = cjson.encode(state)
redis.call('SET', KEYS[1], encoded, 'EX', ARGV[3])
return encoded
