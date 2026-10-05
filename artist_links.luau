--!nonstrict
-- One clickable name per credited artist. Each name takes a share of the
-- row and elides when the widget is narrower than the full credit line.

local M = {}

-- opts: artists ({ id, name }[]), fontSize, color, key, onOpen(artistId)
function M.line(opts)
  local artists = opts.artists
  local parts = {}
  for index, artist in ipairs(artists) do
    local name = tostring(artist.name) .. (index < #artists and "," or "")
    local id = tostring(artist.id)
    table.insert(parts, ui.column({
      key = opts.key .. "-" .. id,
      flexGrow = 1,
      onClick = function() opts.onOpen(id) end,
    }, {
      ui.label({
        text = name,
        fontSize = opts.fontSize,
        color = opts.color,
        maxLines = 1,
        flexGrow = 1,
      }),
    }))
  end
  return ui.row({ key = opts.key, gap = 4, align = "center", flexGrow = 1 }, parts)
end

return M
