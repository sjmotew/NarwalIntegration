# Room cleaning setup

Required before `vacuum.clean_area` works. Needs Home Assistant 2026.3 or newer.

Home Assistant's room cleaning targets **HA areas**, not the robot's own rooms, so there is a
one-time mapping step. Without it the service fails with
*"Area mapping is not configured for vacuum.&lt;entity&gt;"*.

1. Create a Home Assistant **area** for each room you want to clean (Settings → Areas & Zones), if you don't already have one.
2. Open the **mapping editor** and match each robot segment to its area (see below).
3. `vacuum.clean_area` can then target those areas, and the robot cleans the matching rooms.

## Where the mapping editor lives

It hangs off the **entity**, not the integration. There is **no such option on the integration
page or the device page**; that is the most common place people look and it is not there.

Any of these three routes opens the same editor:

| Route | Where |
|---|---|
| **Entity settings** (most reliable) | Settings → Devices & Services → **Entities** tab → search your vacuum → open it → **cog icon** → *Map vacuum segments to areas* |
| **First-run prompt** | Open the vacuum → **Clean areas** → **Configure** (only shown while no mapping exists, and only to admins) |
| **Header action** | Open the vacuum → **Clean areas** → header action (use this one once a mapping already exists) |

Home Assistant shows the option when the entity's domain is `vacuum` and it advertises the
`CLEAN_AREA` feature. This integration sets that flag, so if the row is missing:

- **Hard-refresh the browser** (<kbd>Ctrl</kbd>+<kbd>Shift</kbd>+<kbd>R</kbd>). The frontend caches aggressively.
- **Check the entity is not `unavailable`.** The row is gated on a live state object, so it will not render while the robot is unreachable.
- Confirm you are on **Home Assistant 2026.3+**, when area mapping was introduced.

Room names come from the robot's map. Rooms you named in the Narwal app keep those names, and
the rest use the shared room-type table corrected in
[#48](https://github.com/sjmotew/NarwalIntegration/pull/48). **Name your rooms in the Narwal
app before mapping**: the mapping keys on segment *id*, so renaming later is safe, but naming
first means your HA areas, the robot's map and the vacuum entity's current-room attribute all
read the same words.

`cleaning_area_id` accepts an **ordered list**, so you can clean several rooms in one job and
the robot follows the order you picked.

> **Remapping resets this.** A fresh full-house map in the Narwal app renumbers segments, which
> invalidates the mapping. The integration detects the change and raises a Home Assistant
> repair issue so you know to redo it.

## Per-room profiles and the dashboard

v1.0.8 added per-room cleaning profiles: six selects and a switch **per room**, 168 entities
on a 24-room map, which is right for automations and wrong for a dashboard. Room profile
controls are disabled by default; selection switches stay enabled but hidden so a saved
selection can never become an unreachable state.

Enable the room profile entities you actually use, then run
[`tools/gen_dashboard.py`](../tools/gen_dashboard.py) against your Home Assistant entity
registry. It emits a **room-picker section**: one dropdown, and only the chosen room's controls
on screen, plus a Whole-house panel for the defaults, Start / Clear-selection buttons, a
`script` that cleans exactly one room with its own profile, and a Dock-tasks section. Paste the
YAML into any sections-view dashboard. Needs the `state-switch` card from HACS.

## `narwal.clean_rooms`

Start a room clean with stable service keys such as `vacuum_and_mop`, `wet`,
`meticulous`, and `super_powerful`. `rooms` accepts robot room IDs or `"all"`.
`passes` is a number from 1 to 3. This service sends a clean command; it does not
use the saved per-room profiles. Use `vacuum.start` to apply those profiles and
the saved room selection.

## Reset next-clean profiles

`narwal.set_pending_profiles` updates one docked robot's pending whole-floor
profile, supplied room profiles and optional final room selection together.
It sends no robot commands: it does not wake the robot, fetch a map or start a
clean. A scheduled reset therefore cannot interleave a series of select calls
with a starting clean or accidentally change live suction/water settings.
Room switches and selects do not need to be enabled or present.

```yaml
action: narwal.set_pending_profiles
target:
  entity_id: vacuum.narwal_flow_vacuum
data:
  global_profile:
    mode: vacuum_and_mop
    suction: standard
    water: normal
    mop_strength: normal
    passes: 1
    route: meticulous
  room_profiles:
    "4":
      mode: vacuum
      suction: strong
      passes: 2
      route: standard
    Kitchen:
      mode: mop
      water: wet
      mop_strength: high
      passes: 1
      route: meticulous
  selected_rooms: []
```

Use your vacuum entity and current-map room IDs or exact, case-sensitive display
names (including spaces). Numeric strings mean IDs. Unknown rooms, ambiguous
names, or two keys naming the same room reject the whole request. Use IDs if names
are duplicated; the example assumes `Kitchen` is a different room from ID 4.

- `global_profile` and `room_profiles` are required. Each supplied profile must
  contain `mode`, integer `passes` (1, 2 or 3), and `route`.
- `mode`: `vacuum`, `mop`, `vacuum_then_mop`, `vacuum_and_mop`.
- Vacuum modes require `suction`: `ai`, `quiet`, `standard`, `strong`,
  `super_powerful`, or `ultra_powerful` (where supported by the model).
  The `clean_rooms` compatibility keys `normal`, `super`, `ultra`, and `max`
  have the same meanings here. Mop-only profiles must omit suction.
- Mop modes require `water` (`dry`, `normal`, `wet`) and `mop_strength`
  (`normal`, `high`). Vacuum-only profiles must omit both.
- `route`: `standard` or `meticulous`. Display labels such as `Wet`, `High`,
  `Vacuum and mop`, and string passes such as `"2"` are not accepted.
- Omitted rooms retain their stored profiles and overrides; `room_profiles: {}`
  retains all of them. Fields that already inherit whole-floor defaults continue
  to inherit them. Mode-inapplicable settings retain their stored values.
- Omit `selected_rooms` to preserve the selection. `selected_rooms: []` clears
  it, so the next `vacuum.start` uses the whole floor. A nonempty list replaces
  the selection and may include rooms omitted from `room_profiles`.
- Room order and other maps' profiles/selections are preserved. Selection order
  does not change the saved room cleaning order.

The profile stores and a positively identified current map must already be loaded.
The robot must report idle dock presence; active, paused, returning, unknown or
error states reject the request. All profiles are validated before any change.
The service takes the same lock as clean starts, then rechecks state, map identity,
room IDs and names, including omitted rooms. A changed map rejects the entire
update. Updating local defaults preserves any existing telemetry failure; it does
not make unavailable telemetry fresh.

Atomicity is in memory. Persistence is scheduled through the existing stores and
entity restoration; this is not a crash-atomic disk transaction.

## Example: scheduled multi-room clean with settings

Clean settings are read **at the moment the job is dispatched**, so an automation sets the
entities first and calls `vacuum.clean_area` last. Requested on
[#83](https://github.com/sjmotew/NarwalIntegration/issues/83).

```yaml
alias: Narwal — weekday morning clean
description: Kitchen then hallway, vacuum + mop, two passes
triggers:
  - trigger: time
    at: "09:30:00"
conditions:
  - condition: time
    weekday: [mon, tue, wed, thu, fri]
  # Don't start if the robot is already busy or has a fault
  - condition: state
    entity_id: vacuum.narwal_flow_vacuum
    state: docked
actions:
  # 1. Settings first — these are read when the job is dispatched
  - action: select.select_option
    target:
      entity_id: select.narwal_flow_clean_mode
    data:
      option: Vacuum and mop

  - action: select.select_option
    target:
      entity_id: select.narwal_flow_mopping_humidity
    data:
      option: Slightly wet

  - action: select.select_option
    target:
      entity_id: select.narwal_flow_mop_strength
    data:
      option: High

  - action: number.set_value
    target:
      entity_id: number.narwal_flow_cleaning_passes
    data:
      value: 2

  - action: vacuum.set_fan_speed
    target:
      entity_id: vacuum.narwal_flow_vacuum
    data:
      fan_speed: Strong

  # 2. Then dispatch the rooms, in the order you want them cleaned
  - action: vacuum.clean_area
    target:
      entity_id: vacuum.narwal_flow_vacuum
    data:
      cleaning_area_id:
        - kitchen
        - hallway
mode: single
```

Notes:

- **Substitute your own entity ids.** They are derived from your device name, so yours will
  differ; check Settings → Devices & Services → Entities.
- `cleaning_area_id` takes **HA area ids**, not robot room names, and the segment-to-area
  mapping above must exist first.
- The clean-settings entities apply to the *next* job. Changing them mid-clean does not alter
  the job in progress.
- `select.select_option` takes the **displayed** option (`Vacuum and mop`), not the underlying
  key (`vacuum_and_mop`).
- Fan speed is a separate call because it lives on the vacuum entity rather than in the
  clean-settings block.
- Omit the settings steps you don't care about; whatever you leave alone keeps its current
  value.
