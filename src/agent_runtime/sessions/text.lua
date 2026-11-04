-- Latest provisional snapshot only; ordering uses decimal strings without precision loss.
local function older(a, b)
    return #a < #b or (#a == #b and a < b)
end
local raw = redis.call('GET', KEYS[1])
if ARGV[1] == 'read' then return raw or false end
local incoming = cjson.decode(ARGV[2])
if raw then
    local current = cjson.decode(raw)
    if older(incoming.step, current.step) then return false end
    if incoming.step == current.step then
        if older(incoming.attempt, current.attempt) then return false end
        if incoming.attempt == current.attempt and
            not older(current.sequence, incoming.sequence) then return false end
    end
end
redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
return ARGV[2]
