--!nonstrict
-- One clickable name per credited artist. A row never shrinks a label, so
-- each name gets its share of the width as maxWidth and elides inside it.

local CHAR_EM = 0.6   -- rough glyph width, in units of the font size

local M = {}

-- opts: artists ({ id, name }[]), width (px for the whole line), fontSize,
-- color, key, onOpen(artistId)
function M.line(opts)
  local artists = opts.artists
  local size = opts.fontSize
  local gap = 4
  local free = opts.width - gap * (#artists - 1)

  local names, lengths, total = {}, {}, 0
  for index, artist in ipairs(artists) do
    -- The comma rides on the name before it: a label of its own would get
    -- the gap on both sides.
    names[index] = tostring(artist.name) .. (index < #artists and "," or "")
    lengths[index] = utf8.len(names[index]) or #names[index]
    total += lengths[index]
  end
  local fits = total * size * CHAR_EM <= free

  local parts = {}
  for index, artist in ipairs(artists) do
    local share = fits and free or math.max(24, math.floor(free * lengths[index] / total))
    local id = tostring(artist.id)
    table.insert(parts, ui.column({
      key = opts.key .. "-" .. id,
      onClick = function() opts.onOpen(id) end,
    }, {
      ui.label({ text = names[index], fontSize = size, color = opts.color,
                 maxLines = 1, maxWidth = share }),
    }))
  end
  return ui.row({ key = opts.key, gap = gap, align = "center" }, parts)
end

return M
