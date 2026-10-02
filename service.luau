--!nonstrict
-- Keeps scripts/ym_daemon.py alive and republishes its state file into
-- noctalia.state, so the widget itself never touches the network.

local STATE_KEY = "ym_state"
local STATE_FILE = "state.json"
local ALIVE_FILE = "service.alive"
local HEARTBEAT_GRACE = 25   -- seconds without a heartbeat before we restart it
local HUNG_GRACE = 90        -- seconds of stale state before we kill the daemon

local starting = false
local lastStart = 0
local lastBeat = 0
local retryDelay = 15   -- seconds; stretched while the token is missing
local staleSince = nil
local killing = false

local function dataDir()
  local dir = noctalia.pluginDataDir()
  return dir
end

local function pythonPath()
  local dir = noctalia.pluginDir()
  return (dir and (dir .. "/") or "") .. ".venv/bin/python"
end

local function scriptPath()
  local dir = noctalia.pluginDir()
  return (dir and (dir .. "/") or "") .. "scripts/ym_daemon.py"
end

local function tokenFile()
  local value = noctalia.getConfig("token_file")
  if type(value) == "string" and value ~= "" then
    return value
  end
  return "~/.config/noctalia/yandex-music-token"
end

local function publish(state)
  noctalia.state.set(STATE_KEY, state)
end

local function startDaemon()
  local dir = dataDir()
  if not dir or starting then return end
  local now = noctalia.nowMs() / 1000
  if now - lastStart < retryDelay then return end   -- never spin on a failing daemon
  starting = true
  lastStart = now

  local source = noctalia.getConfig("source")
  local volume = tonumber(noctalia.getConfig("volume")) or 70

  noctalia.log("yandex-music: запускаю демон")
  -- No callback: this is a long-lived process, not a one-shot command.
  noctalia.runAsync({
    pythonPath(), scriptPath(),
    "--token-file", tokenFile(),
    "--data-dir", dir,
    "--source", (source == "likes") and "likes" or "wave",
    "--volume", tostring(math.clamp(volume, 0, 100)),
  })
  starting = false
end

local function killHungDaemon()
  -- A hung daemon still holds its lock, so every fresh copy just steps aside.
  if killing then return end
  killing = true
  noctalia.log("yandex-music: демон не отвечает, убиваю и запускаю заново")
  -- "[.]" keeps the pattern from matching this very sh command line.
  noctalia.runAsync({
    "sh", "-c",
    "pkill -f 'scripts/ym_daemon[.]py'; sleep 2; pkill -9 -f 'scripts/ym_daemon[.]py'; true",
  }, function()
    killing = false
    staleSince = nil
    lastStart = 0
    startDaemon()
  end)
end

local function readState()
  local dir = dataDir()
  if not dir then return nil end
  local raw = noctalia.readFile(dir .. "/" .. STATE_FILE)
  if not raw or raw == "" then return nil end
  local decoded = noctalia.json.decode(raw)
  return type(decoded) == "table" and decoded or nil
end

local function beat()
  -- The daemon outlives us otherwise: onExit is not delivered on every way
  -- out, so instead we leave a mark and the daemon stops when it goes stale.
  local now = noctalia.nowMs()
  if now - lastBeat < 2000 then return end
  lastBeat = now
  local dir = dataDir()
  if dir then
    noctalia.writeFile(dir .. "/" .. ALIVE_FILE, tostring(math.floor(now / 1000)))
  end
end

local function tick()
  beat()
  local state = readState()
  if state == nil then
    publish({ ok = false, error = "starting" })
    startDaemon()
    return
  end

  local age = os.time() - (tonumber(state.heartbeat) or 0)
  state.stale = age > HEARTBEAT_GRACE
  publish(state)

  -- Without a token the daemon exits at once, so retry rarely - but do retry,
  -- otherwise it would never notice the token appearing after `ym_auth.py`.
  retryDelay = state.error == "no-token" and 60 or 15

  if not state.stale or state.error == "no-token" then
    staleSince = nil
    if state.stale then startDaemon() end
    return
  end

  local now = noctalia.nowMs() / 1000
  staleSince = staleSince or now
  if now - staleSince > HUNG_GRACE then
    killHungDaemon()
  else
    startDaemon()
  end
end

-- Reading one small file: cheap enough to do it often, and the card should
-- follow a button press rather than trail a second behind it.
noctalia.setUpdateInterval(250)
publish({ ok = false, error = "starting" })
startDaemon()
tick()

function update()
  tick()
end

function onEnable()
  startDaemon()
end

function onConfigChanged()
  -- Source and volume are start-up arguments, so the daemon has to come back.
  noctalia.runAsync({ "pkill", "-f", "scripts/ym_daemon.py" }, function()
    lastStart = 0
    startDaemon()
  end)
end

function onIpc(event)
  if event == "restart" then
    lastStart = 0
    startDaemon()
  end
end

function onExit(_signal, reason)
  -- Disabling the plugin used to leave the daemon playing, and its lock then
  -- kept the next one from ever taking over. A reload is different: the
  -- daemon should survive it, music and all.
  if reason == "reload" then return end
  local dir = dataDir()
  if not dir then return end
  local payload = noctalia.json.encode({
    action = "quit",
    nonce = noctalia.nowMs(),
  })
  if payload then
    noctalia.writeFile(dir .. "/command.json", payload)
  end
end
