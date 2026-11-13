# v1.0.12 changes

## New

- `narwal.set_pending_profiles` updates whole-floor and room defaults together,
  with an optional room selection, without sending commands to the robot.
  Updates are validated against the current map and reject active or uncertain
  task state. See [pending profiles](ROOM-CLEANING.md#reset-next-clean-profiles).

## Breaking

The suction selects no longer list the duplicate `Super` and `Ultra` options. Use `Super Powerful` and `Ultra Powerful` instead. Update automations that pass the old select labels.

`vacuum.set_fan_speed` retains its aliases: `Super` maps to Super Powerful, and `Ultra`/`max` map to Ultra Powerful on five-tier models. During a clean, an Ultra request is clamped to Super Powerful for the live command; the requested Ultra level remains the pending whole-floor default.

The live option list only offers supported transitions. AI and Ultra remain visible when already confirmed as the active setting; when active suction is unknown, its state is unknown rather than inferred from the next-clean default.
