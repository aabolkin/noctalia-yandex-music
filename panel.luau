--!nonstrict
-- Search panel: type a query, pick a track, the daemon plays it.
-- Text input only lives in panels, so the desktop card opens this one.

local artistLinks = require("./artist_links.luau")

local STATE_KEY = "ym_state"
local OPEN_KEY = "ym_open"     -- the desktop card asks us to open a collection
local OPEN_FRESH_MS = 5000     -- an older request belongs to a previous session
local COMMAND_FILE = "command.json"
local ROW_HEIGHT = 42
local COVER = 30
-- What a track row leaves for its text: the row is about 460px, minus the
-- cover, the duration, three buttons and the gaps between them.
local ARTIST_LINE_WIDTH = 220
-- A collection can hold hundreds of tracks: the list grows by a chunk as the
-- wheel nears its end instead of building every row up front.
local LIST_CHUNK = 60
local LOAD_AHEAD_PX = 400
-- The row is about 460px and each arrow takes some 32px, which leaves about
-- 380px for five tiles and their gaps.
local ALBUM_TILE = 64
local ALBUM_PAGE = 5

local state = noctalia.state.get(STATE_KEY) or {}
local nonce = 0
local typed = ""        -- what is in the field right now
local asked = ""        -- the last query actually sent to the daemon
local seed = 0          -- bumped to re-seed the (uncontrolled) input
local selected = 1      -- 1-based, over hints or over the flat entry list
local shownRows = LIST_CHUNK
local listSeq = 0       -- a new list gets a new scroll, starting from the top
local lastEnter = 0     -- Enter arrives twice: as a chord and as a submit
local hintMoved = false -- has the selection been stepped into the hint list

-- Browsing an opened collection. Its tracks come from collection.json rather
-- than the state: there can be hundreds of them, and the state is rewritten
-- every second. The field filters them here, without touching the network.
local browsing = false
local shelf = { rev = -1, items = {} }
local trail = {}        -- collections the back button returns to
-- Removed from the recent list here at once; the daemon's state catches up
-- a moment later and would otherwise flash the row back.
local forgotten = {}
local albumPage = 1

local render

local function resetList()
  shownRows = LIST_CHUNK
  listSeq += 1
end

local function tr(key, subst)
  return noctalia.tr(key, subst)
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
  end
end

local function clock(ms)
  local total = math.floor((tonumber(ms) or 0) / 1000)
  return string.format("%d:%02d", math.floor(total / 60), total % 60)
end

-- Sections in the order they are shown; the category Yandex calls the best
-- match jumps to the front, so searching a band name leads with the band.
local SECTIONS = {
  { kind = "track", field = "items", header = "search.section.tracks" },
  { kind = "artist", field = "artists", header = "search.section.artists" },
  { kind = "album", field = "albums", header = "search.section.albums" },
  { kind = "playlist", field = "playlists", header = "search.section.playlists" },
}

local function collection()
  return state.collection or {}
end

local function shelfItems()
  local block = collection()
  local rev = tonumber(block.rev) or 0
  if shelf.rev ~= rev then
    -- Re-read only when the daemon says the collection changed: parsing a
    -- hundred kilobytes of JSON on every render would be felt.
    shelf = { rev = rev, items = {} }
    local dir = noctalia.pluginDataDir()
    if dir and tostring(block.status or "") == "ok" then
      local raw = noctalia.readFile(dir .. "/collection.json")
      local decoded = raw and raw ~= "" and noctalia.json.decode(raw) or nil
      if type(decoded) == "table" then shelf.items = decoded end
    end
  end
  return shelf.items
end

local function collectionTitle()
  local block = collection()
  local title = tostring(block.title or "")
  if block.kind == "likes" or title == "likes" then
    return tr("search.likes_all")
  end
  return title
end

-- One flat list for the arrows and the scroll, whatever the rows are.
local function entries()
  local out = {}
  local function push(kind, list, header)
    for _, item in ipairs(list or {}) do
      table.insert(out, { kind = kind, item = item, header = header })
    end
  end

  local found = state.search or {}

  if browsing then
    -- Inside a collection the field filters what is already here: there is no
    -- finding one song among hundreds by paging.
    local needle = typed:lower()
    for _, item in ipairs(shelfItems()) do
      local hay = (tostring(item.title or "") .. " " .. tostring(item.artist or "")):lower()
      if needle == "" or string.find(hay, needle, 1, true) then
        table.insert(out, { kind = "shelf", item = item })
      end
    end
    return out
  end

  if typed == "" then
    -- An empty field means the previous results belong to an older question:
    -- offer the collections, the last queries and the newest likes instead.
    push("wave", { { id = "wave", title = tr("search.wave") } },
         tr("search.section.collections"))
    push("likes_all", { { id = "likes", title = tr("search.likes_all"),
                          count = tonumber(state.likesCount) or 0 } },
         tr("search.section.collections"))
    -- Only a few, so that the likes below them still show without scrolling.
    local shown = 0
    for _, query in ipairs(state.history or {}) do
      if shown >= 4 then break end
      if not forgotten[query] then
        shown += 1
        push("query", { { id = query, title = query } }, tr("search.section.recent"))
      end
    end
    for _, item in ipairs(state.likesPreview or {}) do
      push("track", { { id = item.id, title = item.title, artist = item.artist,
                        artistId = item.artistId, artists = item.artists,
                        durationMs = item.durationMs,
                        liked = true } },
           tr("search.section.likes"))
    end
    return out
  end

  push("track", found.likes, tr("search.section.likes"))

  local order = {}
  for _, section in ipairs(SECTIONS) do
    if section.kind == tostring(found.best or "") then
      table.insert(order, 1, section)
    else
      table.insert(order, section)
    end
  end
  for _, section in ipairs(order) do
    push(section.kind, found[section.field], tr(section.header))
  end
  return out
end

local function results()
  return entries()
end

-- Hints are shown until the query they belong to has been searched for.
local function hints()
  if browsing then return {} end   -- внутри коллекции фильтр, а не поиск
  if typed == "" or typed == asked then return {} end
  local block = state.suggest or {}
  if tostring(block.query or "") ~= typed then return {} end
  return block.items or {}
end

local function search(query)
  query = tostring(query or "")
  if query == "" then return end
  typed = query
  asked = query
  selected = 1
  resetList()
  hintMoved = false
  forgotten[query] = nil
  send("search", query)
end

local function forgetQuery(query)
  query = tostring(query or "")
  if query == "" then return end
  forgotten[query] = true
  send("forget_query", query)
  selected = 1
  render()
end

-- `nested` keeps the way back: an album opened from its artist returns there.
local function openCollection(what, nested)
  if nested and browsing then
    local block = collection()
    table.insert(trail, tostring(block.kind or "") .. ":" .. tostring(block.id or ""))
  else
    trail = {}
  end
  browsing = true
  typed = ""
  asked = ""
  seed += 1        -- чистое поле под фильтр, и фокус возвращается в него
  selected = 1
  resetList()
  albumPage = 1
  send("open", what)
end

local function openArtist(artistId)
  artistId = tostring(artistId or "")
  if artistId == "" then return end
  openCollection("artist:" .. artistId, true)
  render()
end

local function closeCollection()
  browsing = false
  trail = {}
  typed = ""
  asked = ""
  seed += 1
  selected = 1
  resetList()
end

local function goBack()
  local previous = table.remove(trail)
  if previous == nil then
    closeCollection()
    return
  end
  local saved = trail
  openCollection(previous)
  trail = saved
end

local function useHint(text)
  typed = tostring(text or "")
  hintMoved = false
  seed += 1        -- a fresh key re-seeds the field and takes the keyboard back
  search(typed)
end

local function activateEntry(entry)
  if entry == nil then return end
  local id = tostring(entry.item.id or "")
  if entry.kind == "query" then
    typed = id
    seed += 1
    search(id)
    return
  end
  if entry.kind == "wave" then
    send("wave_play")
    panel.close()
    return
  end
  if entry.kind == "likes_all" then
    openCollection("likes")
    return
  end
  if entry.kind == "shelf" then
    send("collection_play", id)
    panel.close()
    return
  end
  if entry.kind == "track" then
    send("play_id", id)
    panel.close()
    return
  end
  -- An album is something to look through, not something to start blindly.
  openCollection(entry.kind .. ":" .. id)
end

local function activate()
  -- Enter reaches us twice, once per path; the second one must do nothing.
  local now = noctalia.nowMs()
  if now - lastEnter < 300 then return end
  lastEnter = now

  local list = hints()
  if #list > 0 then
    -- Like an address bar: Enter searches what was typed, and a hint is only
    -- taken once it has been stepped onto with the arrows (or clicked).
    if hintMoved then
      useHint(list[math.clamp(selected, 1, #list)])
    else
      search(typed)
    end
    return
  end
  local rows = results()
  if #rows > 0 then
    activateEntry(rows[math.clamp(selected, 1, #rows)])
    return
  end
  search(typed)
end

local function move(step)
  local list = hints()
  local overHints = #list > 0
  if not overHints then list = results() end
  if #list == 0 then return end
  if overHints then
    selected += step
    if selected < 1 then selected = #list end
    if selected > #list then selected = 1 end
    hintMoved = true
  else
    -- No wrapping here: jumping to the last of hundreds of rows would build them all.
    selected = math.clamp(selected + step, 1, #list)
    if selected > shownRows then shownRows += LIST_CHUNK end
  end
  render()
end

-- Props are retained between renders, so a node reused for a different kind of
-- body keeps the old ones: the centred "type a query" column turned the result
-- list into a centred stack of content-sized rows. Distinct keys keep the three
-- bodies apart, and every prop that differs between them is always set.
local function notice(text)
  return ui.column({ key = "body-notice", flexGrow = 1,
                     align = "center", justify = "center" }, {
    ui.label({ text = text, fontSize = 12, color = "on_surface_variant",
               maxLines = 3, textAlign = "center" }),
  })
end

local function hintRow(text, index)
  return ui.row({
    key = "hint-" .. index,
    height = 28,
    gap = 8,
    paddingH = 10,
    radius = 8,
    align = "center",
    fill = (index == selected) and "primary/0.18" or "on_surface/0.00",
    onClick = function() useHint(text) end,
  }, {
    ui.glyph({ name = "search", size = 12, color = "on_surface_variant" }),
    ui.label({ text = tostring(text), fontSize = 11, maxLines = 1 }),
  })
end

local function thumb(item, glyph, round)
  local cover = tostring(item.cover or "")
  if cover ~= "" then
    return ui.image({ path = cover, width = COVER, height = COVER,
                      radius = round and math.floor(COVER / 2) or 6, fit = "cover" })
  end
  return ui.row({ width = COVER, height = COVER, radius = round and 15 or 6,
                  fill = "on_surface/0.08", align = "center", justify = "center" }, {
    ui.glyph({ name = glyph, size = 14, color = "on_surface_variant" }),
  })
end

local function actionButton(key, glyph, tooltip, handler)
  return ui.button({
    key = key,
    glyph = glyph,
    glyphSize = 13,
    variant = "ghost",
    controlSize = "sm",
    tooltip = tooltip,
    onClick = handler,
  })
end

-- The lower half of a track's text. One artist: the whole half opens them.
-- Several: the half is no target itself, every name is one of its own.
local function artistHalf(item, id, line, artistLink)
  local artists = item.artists
  if type(artists) ~= "table" or #artists < 2 then
    return ui.column({ key = "artist-half-" .. id, flexGrow = 1, justify = "start",
                       onClick = function() openArtist(artistLink) end }, { line })
  end
  return ui.column({ key = "artists-half-" .. id, flexGrow = 1, justify = "start" }, {
    artistLinks.line({
      artists = artists,
      width = ARTIST_LINE_WIDTH,
      fontSize = 10,
      color = "on_surface_variant",
      key = "artists-" .. id,
      onOpen = openArtist,
    }),
  })
end

local function entryRow(entry, index)
  local item = entry.item
  local id = tostring(item.id or "")
  if entry.kind == "query" then
    -- Only ui.button takes a right click, so a recent query is a button.
    -- Its key differs from the row keys: a reused row would keep row props.
    return ui.button({
      key = "recent-" .. id,
      text = id,
      glyph = "history",
      glyphSize = 14,
      fontSize = 12,
      variant = "ghost",
      contentAlign = "start",
      height = ROW_HEIGHT,
      selected = index == selected,
      tooltip = tr("search.forget_query"),
      onClick = function() activateEntry(entry) end,
      onRightClick = function() forgetQuery(id) end,
    })
  end
  local playing = (entry.kind == "track" or entry.kind == "shelf")
    and tostring(state.trackId or "") == id
  local children = {}
  local subtitle = tostring(item.artist or "")
  local trailing = {}
  local artistLink = ""   -- set for tracks: their artist name opens the artist

  if entry.kind == "wave" then
    table.insert(children, ui.row({ width = COVER, height = COVER, radius = 6,
                                    fill = "primary/0.18", align = "center",
                                    justify = "center" }, {
      ui.glyph({ name = "wave-sine", size = 15, color = "primary" }),
    }))
    subtitle = tr("search.wave_subtitle")
  elseif entry.kind == "likes_all" then
    table.insert(children, ui.row({ width = COVER, height = COVER, radius = 6,
                                    fill = "primary/0.18", align = "center",
                                    justify = "center" }, {
      ui.glyph({ name = "heart-filled", size = 15, color = "primary" }),
    }))
    subtitle = tr("search.tracks_count", { count = tostring(item.count or 0) })
    table.insert(trailing, actionButton("play-likes", "player-play", tr("search.play"),
      function() send("likes_play"); panel.close() end))
  elseif entry.kind == "artist" then
    table.insert(children, thumb(item, "microphone", true))
    subtitle = ""   -- заголовок секции уже сказал, что это исполнитель
    table.insert(trailing, actionButton("radio-" .. id, "radio", tr("search.radio"),
      function() send("artist_radio", id); panel.close() end))
  elseif entry.kind == "album" then
    table.insert(children, thumb(item, "disc", false))
    local bits = {}
    if subtitle ~= "" then table.insert(bits, subtitle) end
    if tostring(item.year or "") ~= "" then table.insert(bits, tostring(item.year)) end
    if (tonumber(item.count) or 0) > 0 then
      table.insert(bits, tr("search.tracks_count", { count = tostring(item.count) }))
    end
    subtitle = table.concat(bits, " · ")
  elseif entry.kind == "playlist" then
    table.insert(children, thumb(item, "playlist", false))
    local bits = {}
    if subtitle ~= "" then table.insert(bits, subtitle) end
    if (tonumber(item.count) or 0) > 0 then
      table.insert(bits, tr("search.tracks_count", { count = tostring(item.count) }))
    end
    subtitle = table.concat(bits, " · ")
  else
    artistLink = tostring(item.artistId or "")
    local cover = thumb(item, playing and "player-play" or "music", false)
    if artistLink ~= "" then
      cover = ui.row({ key = "play-cover-" .. id,
                       onClick = function() activateEntry(entry) end }, { cover })
    end
    table.insert(children, cover)
    table.insert(trailing, ui.label({ text = clock(item.durationMs), fontSize = 10,
                                      color = "on_surface_variant", maxLines = 1 }))
    -- A context menu would need API 28 and only opens from a right click, so
    -- the three things worth doing with a track are buttons of their own.
    table.insert(trailing, actionButton("like-" .. id,
      item.liked and "heart-filled" or "heart",
      item.liked and tr("search.unlike") or tr("search.like"),
      function() send("like_id", id) end))
    table.insert(trailing, actionButton("queue-" .. id, "playlist-add",
      tr("search.queue"), function() send("queue_id", id) end))
    table.insert(trailing, actionButton("track-radio-" .. id, "radio",
      tr("search.radio"), function() send("radio_id", id); panel.close() end))
  end

  local title = ui.label({ text = tostring(item.title or ""), fontSize = 12,
                           fontWeight = "bold", maxLines = 1 })
  local line = subtitle ~= "" and ui.label({ text = subtitle, fontSize = 10,
                                             color = "on_surface_variant", maxLines = 1 })
  -- A click inside a clickable row goes to the row, so a track that links to
  -- its artist has no row target: the cover and the title half play it, the
  -- artist half opens the artist.
  local split = artistLink ~= "" and line
  if split then
    table.insert(children, ui.column({ flexGrow = 1, height = ROW_HEIGHT, gap = 1 }, {
      -- Columns, not rows: a row sizes to its label and lets a long title run
      -- over the buttons, a column hands it its width and the title elides.
      ui.column({ key = "title-half-" .. id, flexGrow = 1, justify = "end",
                  onClick = function() activateEntry(entry) end }, { title }),
      artistHalf(item, id, line, artistLink),
    }))
  else
    table.insert(children, ui.column({ flexGrow = 1, gap = 1, justify = "center" },
                                     line and { title, line } or { title }))
  end
  for _, node in ipairs(trailing) do
    table.insert(children, node)
  end

  return ui.row({
    -- A stable key keeps hover state on the row it belongs to; split rows get
    -- their own so a retained node never switches between the two layouts.
    key = entry.kind .. "-" .. id .. (split and "-split" or ""),
    height = ROW_HEIGHT,
    gap = 10,
    paddingH = 10,
    radius = 10,
    align = "center",
    fill = (index == selected) and "primary/0.18"
      or (playing and "primary/0.10" or "on_surface/0.04"),
    onClick = split and "" or function() activateEntry(entry) end,
  }, children)
end

local function header()
  if not browsing then
    return ui.row({ key = "head-search", gap = 8, align = "center", height = 24 }, {
      ui.glyph({ name = "search", size = 16, color = "on_surface_variant" }),
      ui.label({ text = tr("search.title"), fontSize = 14, fontWeight = "bold",
                 textAlign = "start" }),
    })
  end
  local block = collection()
  local count = tonumber(block.count) or 0
  return ui.row({ key = "head-shelf", gap = 8, align = "center", height = 24 }, {
    ui.button({ key = "back", glyph = "arrow-left", glyphSize = 14, variant = "ghost",
                controlSize = "sm", tooltip = tr("search.back"),
                onClick = function() goBack(); render() end }),
    ui.label({ text = collectionTitle(), fontSize = 14, fontWeight = "bold",
               textAlign = "start", maxLines = 1 }),
    ui.label({ text = tr("search.tracks_count", { count = tostring(count) }),
               fontSize = 10, color = "on_surface_variant", textAlign = "start" }),
    ui.spacer({ flexGrow = 1 }),
    ui.button({ key = "play-shelf", glyph = "player-play", glyphSize = 14,
                variant = "ghost", controlSize = "sm", tooltip = tr("search.play_all"),
                enabled = count > 0,
                onClick = function() send("collection_play"); panel.close() end }),
  })
end

local function albumTile(item)
  local id = tostring(item.id or "")
  local cover = tostring(item.cover or "")
  local art
  if cover ~= "" then
    art = ui.image({ path = cover, width = ALBUM_TILE, height = ALBUM_TILE,
                     radius = 8, fit = "cover" })
  else
    art = ui.row({ width = ALBUM_TILE, height = ALBUM_TILE, radius = 8,
                   fill = "on_surface/0.08", align = "center", justify = "center" }, {
      ui.glyph({ name = "disc", size = 22, color = "on_surface_variant" }),
    })
  end
  return ui.column({
    key = "album-tile-" .. id,
    width = ALBUM_TILE,
    gap = 3,
    onClick = function()
      openCollection("album:" .. id, true)
      render()
    end,
  }, {
    art,
    ui.label({ text = tostring(item.title or ""), fontSize = 10, fontWeight = "bold",
               maxLines = 1, maxWidth = ALBUM_TILE }),
    ui.label({ text = tostring(item.year or ""), fontSize = 9,
               color = "on_surface_variant", maxLines = 1, maxWidth = ALBUM_TILE }),
  })
end

local function albumStrip()
  local block = collection()
  local albums = block.albums or {}
  if not browsing or block.kind ~= "artist" or #albums == 0 then return nil end
  local pages = math.max(1, math.ceil(#albums / ALBUM_PAGE))
  albumPage = math.clamp(albumPage, 1, pages)

  local tiles = {}
  local first = (albumPage - 1) * ALBUM_PAGE + 1
  for index = first, math.min(first + ALBUM_PAGE - 1, #albums) do
    table.insert(tiles, albumTile(albums[index]))
  end

  return ui.column({ key = "albums", gap = 6, align = "stretch" }, {
    ui.label({ text = `{tr("search.section.albums")} · {#albums}`, fontSize = 9,
               fontWeight = "bold", textAlign = "start", color = "on_surface_variant" }),
    ui.row({ gap = 8, align = "center" }, {
      ui.button({ key = "albums-prev", glyph = "chevron-left", glyphSize = 12,
                  variant = "ghost", controlSize = "sm", enabled = albumPage > 1,
                  onClick = function() albumPage -= 1; render() end }),
      ui.row({ flexGrow = 1, gap = 10, align = "start", justify = "start" }, tiles),
      ui.button({ key = "albums-next", glyph = "chevron-right", glyphSize = 12,
                  variant = "ghost", controlSize = "sm", enabled = albumPage < pages,
                  onClick = function() albumPage += 1; render() end }),
    }),
  })
end

local function listBody(rows)
  local nodes = {}
  local shownHeader = nil
  for index = 1, math.min(shownRows, #rows) do
    local entry = rows[index]
    if entry.header ~= shownHeader then
      shownHeader = entry.header
      table.insert(nodes, ui.label({ text = entry.header, fontSize = 9,
                                     fontWeight = "bold", textAlign = "start",
                                     color = "on_surface_variant" }))
    end
    table.insert(nodes, entryRow(entry, index))
  end

  -- The scroll takes whatever height the album strip leaves, all of it when
  -- there is none.
  local parts = {
    ui.scroll({
      key = "list-" .. listSeq,
      flexGrow = 1,
      gap = 4,
      align = "stretch",
      onScroll = function(offset, maxOffset)
        if shownRows >= #rows then return end
        if (tonumber(maxOffset) or 0) - (tonumber(offset) or 0) < LOAD_AHEAD_PX then
          shownRows += LIST_CHUNK
          render()
        end
      end,
    }, nodes),
  }
  local albums = albumStrip()
  if albums then table.insert(parts, albums) end
  return ui.column({ key = "body-list", flexGrow = 1, gap = 8,
                     align = "stretch", justify = "start" }, parts)
end

render = function()
  local found = state.search or {}
  local status = tostring(found.status or "idle")
  local suggestions = hints()
  local rows = results()

  local block = collection()
  local body
  if browsing and tostring(block.status or "") == "loading" and #rows == 0 then
    body = notice(tr("search.loading"))
  elseif browsing and tostring(block.status or "") == "error" then
    body = notice(tr("search.error", { message = tostring(block.message or "") }))
  elseif browsing and #rows == 0 then
    body = notice(tr("search.empty"))
  elseif #suggestions > 0 then
    local nodes = {}
    for index, text in ipairs(suggestions) do
      table.insert(nodes, hintRow(text, index))
    end
    table.insert(nodes, ui.spacer({ flexGrow = 1 }))
    body = ui.column({ key = "body-hints", flexGrow = 1, gap = 2,
                       align = "stretch", justify = "start" }, nodes)
  elseif #rows > 0 then
    body = listBody(rows)
  elseif status == "loading" then
    body = notice(tr("search.loading"))
  elseif status == "error" then
    body = notice(tr("search.error", { message = tostring(found.message or "") }))
  elseif status == "ok" then
    body = notice(tr("search.empty"))
  else
    body = notice(tr("search.hint"))
  end

  panel.render(ui.column({ flexGrow = 1, gap = 10, padding = 16 }, {
    header(),
    ui.row({ gap = 8, align = "center" }, {
      ui.input({
        key = "query-" .. seed,
        value = typed,
        flexGrow = 1,
        focus = true,
        placeholder = browsing and tr("search.filter") or tr("search.placeholder"),
        controlSize = "md",
        onChange = function(value)
          typed = tostring(value or "")
          selected = 1
          resetList()
          hintMoved = false
          if browsing then
            render()          -- фильтруем уже загруженное, сеть не трогаем
          else
            send("suggest", typed)
          end
        end,
        onSubmit = function(value)
          typed = tostring(value or "")
          activate()
        end,
      }),
      ui.button({
        text = tr("search.button"),
        variant = "primary",
        controlSize = "md",
        visible = not browsing,
        onClick = function() search(typed) end,
      }),
    }),
    body,
  }))
end

-- The state ticks four times a second as the track plays, and nothing here
-- shows the position: re-render only when what the panel draws has changed,
-- so typing is never interrupted by a repaint.
local function fingerprint(value)
  local found = value.search or {}
  local block = value.suggest or {}
  local covers = 0
  for _, field in ipairs({ "items", "artists", "albums", "playlists" }) do
    for _, row in ipairs(found[field] or {}) do
      if tostring(row.cover or "") ~= "" then covers += 1 end
    end
  end
  local shelfAlbums = (value.collection or {}).albums or {}
  local albumCovers = 0
  for _, row in ipairs(shelfAlbums) do
    if tostring(row.cover or "") ~= "" then albumCovers += 1 end
  end
  return table.concat({
    tostring(found.status or ""),
    tostring(found.query or ""),
    tostring(#(found.items or {})),
    tostring(#(found.likes or {})),
    tostring(#(found.artists or {})),
    tostring(#(found.albums or {})),
    tostring(#(found.playlists or {})),
    tostring(covers),
    tostring(found.message or ""),
    tostring(block.query or ""),
    tostring(#(block.items or {})),
    tostring(#(value.history or {})),
    tostring(#(value.likesPreview or {})),
    tostring(value.likesCount or ""),
    tostring((value.collection or {}).rev or ""),
    tostring((value.collection or {}).status or ""),
    tostring(#shelfAlbums),
    tostring(albumCovers),
    tostring(value.trackId or ""),
  }, "|")
end

local stamp = fingerprint(state)

local handledOpen = 0

local function takeOpenRequest(request)
  if type(request) ~= "table" then return end
  local at = tonumber(request.nonce) or 0
  if at <= handledOpen then return end
  handledOpen = at
  local target = tostring(request.target or "")
  if target == "" then return end
  openCollection(target)
  render()
end

render()

-- The panel may be loaded by the very click that asks for an artist, so a
-- fresh request found at start-up is honoured; an old one is not.
do
  local pending = noctalia.state.get(OPEN_KEY)
  if type(pending) == "table" then
    if noctalia.nowMs() - (tonumber(pending.nonce) or 0) < OPEN_FRESH_MS then
      takeOpenRequest(pending)
    else
      handledOpen = tonumber(pending.nonce) or 0
    end
  end
end

noctalia.state.watch(OPEN_KEY, takeOpenRequest)

noctalia.state.watch(STATE_KEY, function(value)
  if type(value) ~= "table" then return end
  state = value
  local next = fingerprint(value)
  if next == stamp then return end
  stamp = next
  render()
end)

function onKey(chord, pressed)
  if not pressed then return end
  if chord == "Up" then
    move(-1)
  elseif chord == "Down" then
    move(1)
  elseif chord == "Return" then
    activate()
  end
end
