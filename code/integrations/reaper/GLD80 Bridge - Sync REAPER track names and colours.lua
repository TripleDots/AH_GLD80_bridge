-- @description GLD80 Bridge - Optional REAPER Send, exact Pan, FX and labels
-- @version 1.23
-- @author GLD80 MCU Bridge community project
-- @about
--   Optional REAPER helper for GLD80 MCU Bridge v0.6.41+.
--   Faders, Bank/Channel, transport and track buttons stay on standard MCU.
--   REAPER stock MCU does not implement Send assignment, so this helper adds
--   selected-Send motor-fader flip, GAIN Send-slot selection, exact Pan, FX pages, names and colours.

local MAX_TRACKS = 32
local PLUGIN_SLOTS = 8
local POLL_INTERVAL = 0.01
local HEARTBEAT_INTERVAL = 1.0

local SNAPSHOT_NAME = "gld80_mcu_bridge_reaper.tsv"
local BANK_COMMAND_NAME = "gld80_mcu_bridge_reaper_bank.tsv"
local PAN_COMMAND_PREFIX = "gld80_mcu_bridge_reaper_pan_"
local PLUGIN_COMMAND_NAME = "gld80_mcu_bridge_reaper_plugin.tsv"
local PLUGIN_PARAMETER_PREFIX = "gld80_mcu_bridge_reaper_plugin_param_"
local SEND_DELTA_PREFIX = "gld80_mcu_bridge_reaper_send_delta_"
local SEND_FADER_PREFIX = "gld80_mcu_bridge_reaper_send_fader_"
local TRACK_FADER_PREFIX = "gld80_mcu_bridge_reaper_track_fader_"

local SNAPSHOT_MAGIC = "GLD80_REAPER_SYNC"
local SNAPSHOT_VERSION = 17
local BANK_MAGIC = "GLD80_REAPER_BANK"
local PAN_MAGIC = "GLD80_REAPER_PAN"
local PLUGIN_MAGIC = "GLD80_REAPER_PLUGIN"
local PLUGIN_PARAMETER_MAGIC = "GLD80_REAPER_PLUGIN_PARAM"
local SEND_DELTA_MAGIC = "GLD80_REAPER_SEND_DELTA"
local SEND_FADER_MAGIC = "GLD80_REAPER_SEND_FADER"
local TRACK_FADER_MAGIC = "GLD80_REAPER_TRACK_FADER"

local function temporary_directory()
  local path = os.getenv("TEMP") or os.getenv("TMP") or os.getenv("TMPDIR")
  if path and path ~= "" then return path end
  if reaper.GetOS():match("Win") then return reaper.GetResourcePath() end
  return "/tmp"
end

local separator = package.config:sub(1, 1)
local temp_dir = temporary_directory()
local output_path = temp_dir .. separator .. SNAPSHOT_NAME
local temporary_path = output_path .. ".tmp"
local bank_command_path = temp_dir .. separator .. BANK_COMMAND_NAME
local plugin_command_path = temp_dir .. separator .. PLUGIN_COMMAND_NAME

local track_offset = 0
local last_bank_sequence = 0
local last_plugin_sequence = 0
local last_pan_sequence = {}
local last_plugin_parameter_sequence = {}
local last_send_session = {}
local last_send_position = {}
local last_send_sequence = {}
local last_send_fader_sequence = {}
local last_track_fader_sequence = {}
local snapshot_sequence = 0
local last_snapshot_state = ""
local last_write = 0.0
local last_poll = 0.0

local plugin_mode = "tracks" -- tracks | send_fader | plugin_list | plugin_params
local plugin_selected_track = -1
local plugin_selected_fx = -1
local plugin_fx_page = 0
local plugin_param_page = 0
local plugin_selected_send_slot = 0

local _, _, section_id, command_id = reaper.get_action_context()
local INSTANCE_SECTION = "GLD80_MCU_BRIDGE"
local INSTANCE_KEY = "reaper_companion_instance"
local instance_id = string.format(
  "%d:%.9f:%s:%s", os.time(), reaper.time_precise(),
  tostring(command_id or -1), tostring({})
):gsub("[\t\r\n]", "_")
reaper.SetExtState(INSTANCE_SECTION, INSTANCE_KEY, instance_id, false)

local function is_active_instance()
  return reaper.GetExtState(INSTANCE_SECTION, INSTANCE_KEY) == instance_id
end

local function clean_text(value)
  return tostring(value or ""):gsub("[\t\r\n]", " ")
end

local function read_all(path)
  local file = io.open(path, "rb")
  if not file then return nil end
  local text = file:read("*a")
  file:close()
  return text
end

local function current_project_id()
  local project, filename = reaper.EnumProjects(-1, "")
  return clean_text(tostring(project or "") .. "|" .. tostring(filename or ""))
end

local function clamp_offset(value)
  local total = reaper.CountTracks(0)
  if total <= 0 then return 0 end
  return math.max(0, math.min(total - 1, math.floor(tonumber(value) or 0)))
end

local function selected_track_index()
  local track = reaper.GetSelectedTrack(0, 0)
  if not track then return -1 end
  local number = reaper.GetMediaTrackInfo_Value(track, "IP_TRACKNUMBER")
  return math.max(-1, math.floor((tonumber(number) or 0) - 1))
end

local function plugin_track()
  if plugin_selected_track < 0 then return nil end
  return reaper.GetTrack(0, plugin_selected_track)
end

local function pan_to_value7(pan)
  pan = math.max(-1.0, math.min(1.0, tonumber(pan) or 0.0))
  return math.max(0, math.min(127, math.floor(((pan + 1.0) * 63.5) + 0.5)))
end

local function value7_to_pan(value)
  value = math.max(0, math.min(127, math.floor(tonumber(value) or 64)))
  return (value / 63.5) - 1.0
end

local function track_colour(track)
  if not track then return 255, 255, 255, 0 end
  local native = reaper.GetTrackColor(track)
  if not native or native == 0 then return 255, 255, 255, 0 end
  local red, green, blue = reaper.ColorFromNative(native)
  return red or 255, green or 255, blue or 255, 1
end

local function pan_command_path(index)
  return temp_dir .. separator .. string.format("%s%02d.tsv", PAN_COMMAND_PREFIX, index + 1)
end

local function plugin_parameter_path(index)
  return temp_dir .. separator .. string.format("%s%02d.tsv", PLUGIN_PARAMETER_PREFIX, index + 1)
end

local function send_delta_path(index)
  return temp_dir .. separator .. string.format("%s%02d.tsv", SEND_DELTA_PREFIX, index + 1)
end

local function send_fader_path(index)
  return temp_dir .. separator .. string.format("%s%02d.tsv", SEND_FADER_PREFIX, index + 1)
end

local function track_fader_path(index)
  return temp_dir .. separator .. string.format("%s%02d.tsv", TRACK_FADER_PREFIX, index + 1)
end

local function remove_legacy_and_stale_commands()
  os.remove(bank_command_path)
  os.remove(plugin_command_path)
  os.remove(temp_dir .. separator .. "gld80_mcu_bridge_reaper_send.tsv")
  for index = 0, MAX_TRACKS - 1 do
    os.remove(pan_command_path(index))
    os.remove(temp_dir .. separator .. string.format("gld80_mcu_bridge_reaper_send_level_%02d.tsv", index + 1))
    os.remove(send_delta_path(index))
    os.remove(send_fader_path(index))
    os.remove(track_fader_path(index))
  end
  for index = 0, PLUGIN_SLOTS - 1 do
    os.remove(plugin_parameter_path(index))
  end
end

local function consume_bank_command()
  local text = read_all(bank_command_path)
  if not text then return false end
  os.remove(bank_command_path)
  local magic, version, sequence, offset = text:match("^([^\t]+)\t(%d+)\t(%d+)\t([%-]?%d+)")
  if magic ~= BANK_MAGIC or tonumber(version) ~= 1 then return false end
  sequence = tonumber(sequence) or 0
  if sequence <= last_bank_sequence then return false end
  last_bank_sequence = sequence
  track_offset = clamp_offset(offset)
  return true
end

local function clamp_plugin_pages()
  local track = plugin_track()
  if not track then
    plugin_selected_fx = -1
    plugin_fx_page = 0
    plugin_param_page = 0
    return
  end
  local fx_count = reaper.TrackFX_GetCount(track)
  local max_fx_page = math.max(0, math.floor(math.max(0, fx_count - 1) / PLUGIN_SLOTS))
  plugin_fx_page = math.max(0, math.min(max_fx_page, plugin_fx_page))
  if plugin_selected_fx >= fx_count then plugin_selected_fx = fx_count - 1 end
  if plugin_selected_fx < 0 then
    plugin_param_page = 0
    return
  end
  local param_count = reaper.TrackFX_GetNumParams(track, plugin_selected_fx)
  local max_param_page = math.max(0, math.floor(math.max(0, param_count - 1) / PLUGIN_SLOTS))
  plugin_param_page = math.max(0, math.min(max_param_page, plugin_param_page))
end

local function enter_plugin_list()
  local index = selected_track_index()
  if index < 0 then index = clamp_offset(track_offset) end
  plugin_selected_track = index
  plugin_selected_fx = -1
  plugin_fx_page = 0
  plugin_param_page = 0
  plugin_mode = "plugin_list"
  clamp_plugin_pages()
end

local function max_visible_send_slot()
  local total = reaper.CountTracks(0)
  local count = math.min(MAX_TRACKS, math.max(0, total - track_offset))
  local max_slot = 0
  for index = 0, count - 1 do
    local track = reaper.GetTrack(0, track_offset + index)
    if track then
      max_slot = math.max(max_slot, reaper.GetTrackNumSends(track, 0) - 1)
    end
  end
  return math.max(0, max_slot)
end

local function consume_plugin_command()
  local text = read_all(plugin_command_path)
  if not text then return false end
  os.remove(plugin_command_path)
  local magic, version, sequence, action, value = text:match(
    "^([^\t]+)\t(%d+)\t(%d+)\t([^\t\r\n]+)\t([%-]?%d+)"
  )
  if magic ~= PLUGIN_MAGIC or tonumber(version) ~= 1 then return false end
  sequence = tonumber(sequence) or 0
  if sequence <= last_plugin_sequence then return false end
  last_plugin_sequence = sequence
  value = tonumber(value) or 0

  if action == "send_flip_on" then
    plugin_mode = "send_fader"
    plugin_selected_fx = -1
    plugin_fx_page = 0
    plugin_param_page = 0
  elseif action == "send_flip_off" then
    plugin_mode = "tracks"
    plugin_selected_fx = -1
    plugin_fx_page = 0
    plugin_param_page = 0
  elseif action == "send_prev" then
    plugin_selected_send_slot = math.max(0, plugin_selected_send_slot - 1)
  elseif action == "send_next" then
    plugin_selected_send_slot = math.min(
      max_visible_send_slot(), plugin_selected_send_slot + 1
    )
  elseif action == "plugin" or action == "toggle" then
    enter_plugin_list()
  elseif action == "exit" then
    plugin_mode = "tracks"
    plugin_selected_fx = -1
    plugin_fx_page = 0
    plugin_param_page = 0
  elseif (action == "select" or action == "vpot_push") and plugin_mode == "plugin_list" then
    local track = plugin_track()
    local fx = plugin_fx_page * PLUGIN_SLOTS + math.max(0, math.min(7, value))
    if track and fx < reaper.TrackFX_GetCount(track) then
      plugin_selected_fx = fx
      plugin_param_page = 0
      plugin_mode = "plugin_params"
      reaper.TrackFX_Show(track, plugin_selected_fx, 3)
    end
  elseif action == "vpot_push" and plugin_mode == "plugin_params" then
    local track = plugin_track()
    if track and plugin_selected_fx >= 0 then
      reaper.TrackFX_Show(track, plugin_selected_fx, 3)
    end
  elseif action == "bank_left" or action == "bank_right" then
    local direction = action == "bank_right" and 1 or -1
    if plugin_mode == "plugin_list" then
      plugin_fx_page = plugin_fx_page + direction
    elseif plugin_mode == "plugin_params" then
      plugin_param_page = plugin_param_page + direction
    end
  elseif action == "channel_left" or action == "channel_right" then
    if plugin_mode == "plugin_params" then
      local track = plugin_track()
      local direction = action == "channel_right" and 1 or -1
      local target = plugin_selected_fx + direction
      if track and target >= 0 and target < reaper.TrackFX_GetCount(track) then
        plugin_selected_fx = target
        plugin_param_page = 0
        reaper.TrackFX_Show(track, plugin_selected_fx, 3)
      end
    end
  end
  clamp_plugin_pages()
  return true
end

local function apply_pan_commands()
  if plugin_mode ~= "tracks" then return false end
  local changed = false
  for index = 0, MAX_TRACKS - 1 do
    local path = pan_command_path(index)
    local text = read_all(path)
    if text then
      os.remove(path)
      local magic, version, sequence, value = text:match(
        "^([^\t]+)\t(%d+)\t(%d+)\t([%-]?%d+)"
      )
      if magic == PAN_MAGIC and tonumber(version) == 1 then
        sequence = tonumber(sequence) or 0
        if sequence > (last_pan_sequence[index] or 0) then
          last_pan_sequence[index] = sequence
          local track = reaper.GetTrack(0, track_offset + index)
          if track then
            local target = value7_to_pan(value)
            local current = reaper.GetMediaTrackInfo_Value(track, "D_PAN")
            if math.abs(current - target) > 0.000001 then
              reaper.SetMediaTrackInfo_Value(track, "D_PAN", target)
              changed = true
            end
          end
        end
      end
    end
  end
  return changed
end

local function volume_to_fader7(volume)
  -- GLD faders and Send levels share the documented 7-bit taper:
  -- 0 = -inf, 107 = 0 dB and 127 = +10 dB.  Every finite value uses
  -- dB = value * 64 / 127 - 54, so the first step above -inf is about
  -- -53.5 dB.  Keeping the companion in this exact domain prevents hidden
  -- REAPER values below the surface range from trapping the Gain rotary.
  volume = tonumber(volume) or 0.0
  if volume <= 0.0 then return 0 end
  local db = 20.0 * (math.log(volume) / math.log(10.0))
  if db <= -54.0 then return 1 end
  if db >= 10.0 then return 127 end
  return math.max(1, math.min(127,
    math.floor((db + 54.0) * 127.0 / 64.0 + 0.5)))
end

local function fader7_to_volume(value)
  value = math.max(0, math.min(127, math.floor(tonumber(value) or 0)))
  if value <= 0 then return 0.0 end
  local db = value * 64.0 / 127.0 - 54.0
  return 10.0 ^ (db / 20.0)
end

local function apply_send_delta_commands()
  if plugin_mode ~= "tracks" and plugin_mode ~= "send_fader" then return false end
  local changed = false
  for index = 0, MAX_TRACKS - 1 do
    local path = send_delta_path(index)
    local text = read_all(path)
    if text then
      -- Persistent monotonic mailbox: do not delete after reading. Deleting a
      -- path after read can race with the bridge replacing it and discard the
      -- first detent of a direction reversal. Sequence/position fields make
      -- rereading the current file harmless.
      local magic, version, session, sequence, position, immediate_delta = text:match(
        "^([^\t]+)\t(%d+)\t([%-]?%d+)\t([%-]?%d+)\t([%-]?%d+)\t([%-]?%d+)"
      )
      session = tonumber(session) or 0
      sequence = tonumber(sequence) or 0
      position = tonumber(position) or 0
      immediate_delta = tonumber(immediate_delta) or 0
      if magic == SEND_DELTA_MAGIC and tonumber(version) == 2 then
        local previous_session = last_send_session[index]
        local previous_position = last_send_position[index] or 0
        local previous_sequence = last_send_sequence[index] or 0
        if previous_session ~= session then
          -- Seed from the command's own detent count. This prevents a companion
          -- restart from replaying the bridge's entire older cumulative value.
          previous_position = position - immediate_delta
          previous_sequence = 0
          last_send_session[index] = session
        end
        if sequence > previous_sequence then
          local delta = position - previous_position
          last_send_position[index] = position
          last_send_sequence[index] = sequence
          if delta ~= 0 then
            local track = reaper.GetTrack(0, track_offset + index)
            -- Legacy relative-GAIN compatibility: apply movement to the
            -- currently selected existing Send. Missing Sends are never created.
            if track and plugin_selected_send_slot < reaper.GetTrackNumSends(track, 0) then
              local current = reaper.GetTrackSendInfo_Value(
                track, 0, plugin_selected_send_slot, "D_VOL"
              )
              local current_db = -150.0
              if current and current > 0.0000001 then
                current_db = 20.0 * (math.log(current) / math.log(10.0))
              end
              -- REAPER can retain tiny non-zero amplitudes far below its
              -- visible -inf threshold. Adding 0.5 dB from -110..-150 dB then
              -- appears stuck for hundreds of detents. The first clockwise
              -- detent below -90 dB deliberately re-enters at -90 dB.
              if current_db <= -90.0 and delta > 0 then current_db = -90.0 end
              local target_db = math.max(-150.0, math.min(12.0, current_db + delta * 0.5))
              local target = target_db <= -149.0 and 0.0 or (10.0 ^ (target_db / 20.0))
              if math.abs((current or 0.0) - target) > 0.0000001 then
                reaper.SetTrackSendInfo_Value(
                  track, 0, plugin_selected_send_slot, "D_VOL", target
                )
                changed = true
              end
            end
          end
        end
      end
    end
  end
  return changed
end

local function apply_send_fader_commands()
  -- v1.23 applies exact 0..127 motor-fader targets to the currently
  -- selected Send slot. GAIN itself only chooses previous/next Send.
  if plugin_mode ~= "tracks" and plugin_mode ~= "send_fader" then return false end
  local changed = false
  for index = 0, MAX_TRACKS - 1 do
    local path = send_fader_path(index)
    local text = read_all(path)
    if text then
      local magic, version, sequence, value = text:match(
        "^([^\t]+)\t(%d+)\t([%-]?%d+)\t([%-]?%d+)"
      )
      sequence = tonumber(sequence) or 0
      value = math.max(0, math.min(127, tonumber(value) or 0))
      if magic == SEND_FADER_MAGIC and tonumber(version) == 1
          and sequence > (last_send_fader_sequence[index] or 0) then
        last_send_fader_sequence[index] = sequence
        local track = reaper.GetTrack(0, track_offset + index)
        if track and plugin_selected_send_slot < reaper.GetTrackNumSends(track, 0) then
          local target = fader7_to_volume(value)
          local current = reaper.GetTrackSendInfo_Value(
            track, 0, plugin_selected_send_slot, "D_VOL"
          )
          if math.abs((current or 0.0) - target) > 0.0000001 then
            reaper.SetTrackSendInfo_Value(
              track, 0, plugin_selected_send_slot, "D_VOL", target
            )
            changed = true
          end
        end
      end
    end
  end
  return changed
end

local function apply_track_fader_commands()
  -- Legacy compatibility mailbox for profiles that explicitly map GAIN to
  -- hidden normal track volume during a manual Flip. The v0.6.41 default does
  -- not use this path: GAIN selects the Send and the motor fader owns its level.
  if plugin_mode ~= "send_fader" then return false end
  local changed = false
  for index = 0, MAX_TRACKS - 1 do
    local path = track_fader_path(index)
    local text = read_all(path)
    if text then
      local magic, version, sequence, value = text:match(
        "^([^\t]+)\t(%d+)\t([%-]?%d+)\t([%-]?%d+)"
      )
      sequence = tonumber(sequence) or 0
      value = math.max(0, math.min(127, tonumber(value) or 0))
      if magic == TRACK_FADER_MAGIC and tonumber(version) == 1
          and sequence > (last_track_fader_sequence[index] or 0) then
        last_track_fader_sequence[index] = sequence
        local track = reaper.GetTrack(0, track_offset + index)
        if track then
          local target = fader7_to_volume(value)
          local current = reaper.GetMediaTrackInfo_Value(track, "D_VOL")
          if math.abs((current or 0.0) - target) > 0.0000001 then
            reaper.SetMediaTrackInfo_Value(track, "D_VOL", target)
            changed = true
          end
        end
      end
    end
  end
  return changed
end

local function apply_plugin_parameter_commands()
  if plugin_mode ~= "plugin_params" then return false end
  local track = plugin_track()
  if not track or plugin_selected_fx < 0 or plugin_selected_fx >= reaper.TrackFX_GetCount(track) then
    return false
  end
  local changed = false
  local param_count = reaper.TrackFX_GetNumParams(track, plugin_selected_fx)
  for index = 0, PLUGIN_SLOTS - 1 do
    local path = plugin_parameter_path(index)
    local text = read_all(path)
    if text then
      os.remove(path)
      local magic, version, sequence, value = text:match(
        "^([^\t]+)\t(%d+)\t(%d+)\t([%-]?%d+)"
      )
      if magic == PLUGIN_PARAMETER_MAGIC and tonumber(version) == 1 then
        sequence = tonumber(sequence) or 0
        if sequence > (last_plugin_parameter_sequence[index] or 0) then
          last_plugin_parameter_sequence[index] = sequence
          local parameter = plugin_param_page * PLUGIN_SLOTS + index
          if parameter < param_count then
            local target = math.max(0, math.min(127, tonumber(value) or 0)) / 127.0
            local current = reaper.TrackFX_GetParamNormalized(track, plugin_selected_fx, parameter)
            if math.abs(current - target) > 0.000001 then
              reaper.TrackFX_SetParamNormalized(track, plugin_selected_fx, parameter, target)
              changed = true
            end
          end
        end
      end
    end
  end
  return changed
end

local function explicit_track_name(track, project_index)
  if not track then return string.format("Ch%03d", project_index + 1) end
  local ok, name = reaper.GetSetMediaTrackInfo_String(track, "P_NAME", "", false)
  name = ok and clean_text(name) or ""
  name = name:match("^%s*(.-)%s*$") or ""
  if name == "" then return string.format("Ch%03d", project_index + 1) end
  return name
end

local function concise_fx_name(value, fallback_index)
  local name = clean_text(value):match("^%s*(.-)%s*$") or ""
  -- REAPER prefixes FX names with the plug-in format. On the GLD's five
  -- visible characters that reduced every entry to just "VST:".
  name = name:gsub("^[Vv][Ss][Tt]3?[Ii]?%s*:%s*", "")
  name = name:gsub("^[Aa][Uu][Ii]?%s*:%s*", "")
  name = name:gsub("^[Cc][Ll][Aa][Pp]%s*:%s*", "")
  name = name:gsub("^[Jj][Ss]%s*:%s*", "")
  name = name:gsub("^[Dd][Xx][Ii]?%s*:%s*", "")
  name = name:gsub("^[Ll][Vv]2%s*:%s*", "")
  name = name:gsub("%s+%([^()]-%)%s*$", "")
  name = name:match("^%s*(.-)%s*$") or ""
  if name == "" then return string.format("FX %d", fallback_index) end
  return name
end

local function selected_send_value7(track)
  if not track then return 0 end
  if plugin_selected_send_slot >= reaper.GetTrackNumSends(track, 0) then return 0 end
  return volume_to_fader7(reaper.GetTrackSendInfo_Value(
    track, 0, plugin_selected_send_slot, "D_VOL"
  ))
end

local function track_row(surface_index, project_index)
  local track = reaper.GetTrack(0, project_index)
  if not track then return nil end
  local name = explicit_track_name(track, project_index)
  local red, green, blue, custom = track_colour(track)
  local pan7 = pan_to_value7(reaper.GetMediaTrackInfo_Value(track, "D_PAN"))
  local fader7 = volume_to_fader7(reaper.GetMediaTrackInfo_Value(track, "D_VOL"))
  local send7 = selected_send_value7(track)
  return table.concat({surface_index + 1, clean_text(name), red, green, blue, custom, pan7, fader7, send7}, "\t")
end

local function send_fader_row(surface_index, project_index)
  local track = reaper.GetTrack(0, project_index)
  if not track then return nil end
  local name = explicit_track_name(track, project_index)
  local red, green, blue, custom = track_colour(track)
  local send7 = selected_send_value7(track)
  local track_fader7 = volume_to_fader7(reaper.GetMediaTrackInfo_Value(track, "D_VOL"))
  return table.concat({surface_index + 1, clean_text(name), red, green, blue, custom, send7, track_fader7, send7}, "\t")
end

local function plugin_list_row(index, track)
  local red, green, blue, custom = track_colour(track)
  local fx = plugin_fx_page * PLUGIN_SLOTS + index
  local name = "(empty)"
  if track and fx < reaper.TrackFX_GetCount(track) then
    local _, fx_name = reaper.TrackFX_GetFXName(track, fx, "")
    name = concise_fx_name(fx_name, fx + 1)
  end
  return table.concat({index + 1, clean_text(name), red, green, blue, custom, 64}, "\t")
end

local function plugin_parameter_row(index, track)
  local red, green, blue, custom = track_colour(track)
  local name = "(empty)"
  local value7 = 0
  if track and plugin_selected_fx >= 0 and plugin_selected_fx < reaper.TrackFX_GetCount(track) then
    local parameter = plugin_param_page * PLUGIN_SLOTS + index
    local param_count = reaper.TrackFX_GetNumParams(track, plugin_selected_fx)
    if parameter < param_count then
      local _, param_name = reaper.TrackFX_GetParamName(track, plugin_selected_fx, parameter, "")
      name = param_name ~= "" and param_name or string.format("Param %d", parameter + 1)
      value7 = math.max(0, math.min(127, math.floor(
        reaper.TrackFX_GetParamNormalized(track, plugin_selected_fx, parameter) * 127.0 + 0.5
      )))
    end
  end
  return table.concat({index + 1, clean_text(name), red, green, blue, custom, value7}, "\t")
end

local function build_snapshot(sequence)
  local total = reaper.CountTracks(0)
  track_offset = clamp_offset(track_offset)
  clamp_plugin_pages()
  local rows = {}

  if plugin_mode == "tracks" or plugin_mode == "send_fader" then
    local count = math.min(MAX_TRACKS, math.max(0, total - track_offset))
    for index = 0, count - 1 do
      local row = plugin_mode == "send_fader"
        and send_fader_row(index, track_offset + index)
        or track_row(index, track_offset + index)
      if row then rows[#rows + 1] = row end
    end
  else
    local track = plugin_track()
    for index = 0, PLUGIN_SLOTS - 1 do
      if plugin_mode == "plugin_list" then
        rows[#rows + 1] = plugin_list_row(index, track)
      else
        rows[#rows + 1] = plugin_parameter_row(index, track)
      end
    end
  end

  local header = table.concat({
    SNAPSHOT_MAGIC, SNAPSHOT_VERSION, #rows, track_offset, total,
    plugin_mode, plugin_selected_track, plugin_selected_fx,
    plugin_fx_page, plugin_param_page, plugin_selected_send_slot,
    last_bank_sequence, last_plugin_sequence, 0,
    instance_id, math.max(0, math.floor(sequence or 0)), current_project_id()
  }, "\t")
  return header .. "\n" .. table.concat(rows, "\n") .. (#rows > 0 and "\n" or "")
end

local function atomic_write(text)
  local file = io.open(temporary_path, "wb")
  if not file then return false end
  file:write(text)
  file:close()
  -- Windows cannot rename over an existing file. The bridge deliberately keeps
  -- the last valid snapshot through this tiny replace gap, so the status line
  -- and surface do not flicker.
  os.remove(output_path)
  return os.rename(temporary_path, output_path)
end

local function set_toggle_state(on)
  if section_id and command_id and section_id >= 0 and command_id >= 0 then
    reaper.SetToggleCommandState(section_id, command_id, on and 1 or 0)
    reaper.RefreshToolbar2(section_id, command_id)
  end
end

remove_legacy_and_stale_commands()
set_toggle_state(true)

local function loop()
  if not is_active_instance() then return end
  local now = reaper.time_precise()
  if now - last_poll >= POLL_INTERVAL then
    last_poll = now
    local changed = false
    if consume_bank_command() then changed = true end
    if consume_plugin_command() then changed = true end
    if apply_pan_commands() then changed = true end
    if apply_send_delta_commands() then changed = true end
    if apply_send_fader_commands() then changed = true end
    if apply_track_fader_commands() then changed = true end
    if apply_plugin_parameter_commands() then changed = true end

    local state_snapshot = build_snapshot(0)
    if changed or state_snapshot ~= last_snapshot_state or now - last_write >= HEARTBEAT_INTERVAL then
      snapshot_sequence = snapshot_sequence + 1
      local snapshot = build_snapshot(snapshot_sequence)
      if atomic_write(snapshot) then
        last_snapshot_state = state_snapshot
        last_write = now
      end
    end
  end
  reaper.defer(loop)
end

reaper.atexit(function()
  if is_active_instance() then
    reaper.DeleteExtState(INSTANCE_SECTION, INSTANCE_KEY, false)
    set_toggle_state(false)
    os.remove(temporary_path)
    os.remove(output_path)
    os.remove(bank_command_path)
    os.remove(plugin_command_path)
    for index = 0, MAX_TRACKS - 1 do
      os.remove(pan_command_path(index))
      os.remove(send_delta_path(index))
      os.remove(send_fader_path(index))
      os.remove(track_fader_path(index))
    end
    for index = 0, PLUGIN_SLOTS - 1 do os.remove(plugin_parameter_path(index)) end
  end
end)

loop()
