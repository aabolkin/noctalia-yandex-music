--!nonstrict
-- Desktop player card: cover, track, progress and transport buttons.
-- Commands go to the daemon through command.json; it owns the Ynison session.

local artistLinks = require("./artist_links.luau")

local STATE_KEY = "ym_state"
local OPEN_KEY = "ym_open"     -- what the search panel should open when it shows
local PANEL_ID = "alex/yandex-music:search"
local WAVE_STATION = "user:onyourwave"
local COMMAND_FILE = "command.json"

-- The card does not clip its content: a row sized by its children grows past
-- the widget box. So the text column gets a fixed width that fits the 480x144
-- box (12 padding + 112 cover + 12 gap + 320 + 12 padding = 468).
local COVER_SIZE = 112
local CONTENT_WIDTH = 320
-- How far from the current position a slider change has to land to count as a
-- gesture. On a 320px bar this is about four pixels, below aiming precision.
local ECHO_SECONDS = 2

local state = noctalia.state.get(STATE_KEY) or { ok = false, error = "starting" }
local nonce = 0
local seeking = false   -- true while the progress handle is being dragged
local seekTo = 0        -- seconds the handle is pointing at meanwhile
local seekAt = 0        -- when the handle last moved, for the fallback below

local render

local function tr(key, subst)
  return noctalia.tr(key, subst)
end

-- The press goes through a file, a daemon and mpv before it comes back as
-- state, so the card answers straight away and the daemon confirms it.
local function optimistic(action)
  if not state.active then return end
  if action == "toggle" then
    state.paused = not state.paused
  elseif action == "pause" then
    state.paused = true
  elseif action == "resume" or action == "play" then
    state.paused = false
  elseif action == "like_toggle" then
    state.liked = not state.liked
  else
    return
  end
  render()
end

local function send(action, value)
  local dir = noctalia.pluginDataDir()
  if not dir then return end
  nonce += 1
  local payload = noctalia.json.encode({
    action = action,
    value = value,
    nonce = noctalia.nowMs() + nonce,
  })
  if payload then
    noctalia.writeFile(dir .. "/" .. COMMAND_FILE, payload)
    optimistic(action)
  end
end

local function openArtist(artistId)
  artistId = tostring(artistId or "")
  if artistId == "" then artistId = tostring(state.artistId or "") end
  if artistId == "" then return end
  noctalia.state.set(OPEN_KEY, { target = "artist:" .. artistId, nonce = noctalia.nowMs() })
  noctalia.togglePanel(PANEL_ID)
end

local function clock(ms)
  local total = math.floor((tonumber(ms) or 0) / 1000)
  return string.format("%d:%02d", math.floor(total / 60), total % 60)
end

local function button(glyph, action, size, enabled)
  return ui.button({
    glyph = glyph,
    glyphSize = size or 16,
    variant = "ghost",
    controlSize = "sm",
    enabled = enabled ~= false,
    onClick = function() send(action) end,
  })
end

local function waveButton(size)
  return ui.button({
    key = "wave",
    glyph = "wave-sine",
    glyphSize = size,
    variant = "ghost",
    controlSize = "sm",
    selected = state.active == true and state.source == "wave"
      and state.station == WAVE_STATION,
    onClick = function() send("wave_play") end,
  })
end

local function placeholder(message)
  return ui.column({ flexGrow = 1, gap = 6, padding = 12, radius = 14,
                     fill = "surface_variant/0.55", align = "center", justify = "center" }, {
    ui.glyph({ name = "music", size = 20, color = "on_surface_variant" }),
    ui.label({ text = message, fontSize = 11, color = "on_surface_variant",
               maxLines = 3, textAlign = "center" }),
  })
end

render = function()
  if state.ok == false then
    local message = tr("status.starting")
    if state.error == "no-token" then
      message = tr("status.no_token")
    elseif state.error == "session" then
      message = tr("status.error", { message = tostring(state.message or "") })
    end
    desktopWidget.render(placeholder(message))
    return
  end

  -- Nothing loaded yet: one big button that starts the configured source.
  if state.active ~= true then
    desktopWidget.render(ui.column({
      flexGrow = 1, gap = 8, padding = 12, radius = 14,
      fill = "surface_variant/0.55", align = "center", justify = "center",
    }, {
      ui.row({ gap = 6, align = "center", justify = "center" }, {
        ui.button({
          glyph = "player-play",
          glyphSize = 22,
          variant = "ghost",
          controlSize = "lg",
          onClick = function() send("resume") end,
        }),
        waveButton(16),
        ui.button({
          glyph = "search",
          glyphSize = 16,
          variant = "ghost",
          controlSize = "sm",
          onClick = function() noctalia.togglePanel("alex/yandex-music:search") end,
        }),
      }),
      ui.label({ text = tr("status.idle"), fontSize = 11, color = "on_surface_variant" }),
    }))
    return
  end

  local showCover = noctalia.getConfig("show_cover") ~= false and (state.cover or "") ~= ""
  local showProgress = noctalia.getConfig("show_progress") ~= false
  local showLike = noctalia.getConfig("show_like") ~= false

  local details = {}
  table.insert(details, ui.label({
    text = tostring(state.title or ""),
    fontSize = 14,
    fontWeight = "bold",
    maxLines = 1,
    maxWidth = CONTENT_WIDTH,
  }))
  local artists = state.artists
  if type(artists) == "table" and #artists > 1 then
    table.insert(details, artistLinks.line({
      artists = artists,
      width = CONTENT_WIDTH,
      fontSize = 11,
      color = "on_surface_variant",
      key = "artists",
      onOpen = openArtist,
    }))
  else
    table.insert(details, ui.row({ key = "artist", onClick = function() openArtist() end }, {
      ui.label({
        text = tostring(state.artist or ""),
        fontSize = 11,
        color = "on_surface_variant",
        maxLines = 1,
        maxWidth = CONTENT_WIDTH,
      }),
    }))
  end

  if showProgress then
    -- ui.progress takes no clicks, so the bar is a slider: a click jumps the
    -- handle and a drag scrubs. While dragging, the shown time is the draft
    -- one, otherwise the daemon's position would fight the handle.
    local duration = math.max(1, math.floor((tonumber(state.durationMs) or 0) / 1000))
    local position = math.floor((tonumber(state.progressMs) or 0) / 1000)
    local shown = seeking and seekTo or position

    table.insert(details, ui.column({ gap = 2, width = CONTENT_WIDTH }, {
      ui.slider({
        key = "seek",
        width = CONTENT_WIDTH,
        controlSize = "sm",
        min = 0,
        max = duration,
        step = 1,
        value = math.clamp(shown, 0, duration),
        enabled = (state.durationMs or 0) > 0,
        onChange = function(value)
          local target = math.floor(tonumber(value) or 0)
          -- The host re-applies `value` after every render, and that comes
          -- back here as a change. Anything landing on the position we just
          -- drew is that echo, not a gesture: acting on it seeks the player
          -- onto itself again and again, which is audible as stuttering.
          if math.abs(target - position) <= ECHO_SECONDS then return end
          seeking = true
          seekTo = target
          seekAt = noctalia.nowMs()
          render()
        end,
        onDragEnd = function()
          seeking = false
          send("position", seekTo)
        end,
      }),
      -- One label instead of two pinned to the edges: nothing to stretch.
      ui.label({
        text = `{clock(shown * 1000)} / {clock(state.durationMs)}`,
        fontSize = 9,
        color = "on_surface_variant",
        maxWidth = CONTENT_WIDTH,
      }),
    }))
  end

  -- mpv is ours, so the controls are live as long as it is running.
  local steerable = state.hasDevice ~= false

  local controls = {
    button("player-track-prev", "prev", 16, steerable),
    button(state.paused and "player-play" or "player-pause", "toggle", 20, steerable),
    button("player-track-next", "next", 16, steerable),
  }
  if not steerable then
    table.insert(controls, ui.label({
      text = tr("status.no_device"),
      fontSize = 9,
      color = "on_surface_variant",
      maxLines = 1,
      maxWidth = 100,
    }))
  end
  if showLike then
    table.insert(controls, ui.button({
      key = "like",
      glyph = state.liked and "heart-filled" or "heart",
      glyphSize = 14,
      variant = "ghost",
      controlSize = "sm",
      onClick = function() send("like_toggle") end,
    }))
  end
  -- Wave and search pick what to play rather than steer it: they keep apart.
  table.insert(controls, ui.spacer({ key = "controls-gap", flexGrow = 1 }))
  table.insert(controls, waveButton(14))
  table.insert(controls, ui.button({
    key = "search",
    glyph = "search",
    glyphSize = 14,
    variant = "ghost",
    controlSize = "sm",
    onClick = function() noctalia.togglePanel("alex/yandex-music:search") end,
  }))
  table.insert(details, ui.row({ gap = 2, align = "center", width = CONTENT_WIDTH }, controls))

  if (state.commandError or "") ~= "" then
    table.insert(details, ui.label({
      text = tostring(state.commandError),
      fontSize = 9,
      color = "error",
      maxLines = 1,
      maxWidth = CONTENT_WIDTH,
    }))
  end

  local body = { ui.column({ width = CONTENT_WIDTH, gap = 4, justify = "center" }, details) }
  if showCover then
    table.insert(body, 1, ui.row({ key = "cover", onClick = function() openArtist() end }, {
      ui.image({
        path = tostring(state.cover),
        width = COVER_SIZE,
        height = COVER_SIZE,
        radius = 10,
        fit = "cover",
      }),
    }))
  end

  desktopWidget.render(ui.row({
    flexGrow = 1,
    gap = 12,
    padding = 12,
    radius = 14,
    fill = "surface_variant/0.55",
    align = "center",
    justify = "center",
  }, body))
end

render()

noctalia.state.watch(STATE_KEY, function(value)
  if type(value) ~= "table" then return end
  state = value
  -- Should a plain click move the handle without ending a drag, the bar would
  -- stay frozen on the draft value. If it has been still for a moment, take
  -- that as the end of the gesture and seek there.
  if seeking and noctalia.nowMs() - seekAt > 600 then
    seeking = false
    send("position", seekTo)
  end
  render()
end)
