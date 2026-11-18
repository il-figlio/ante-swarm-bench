# Home network

Local-first setup: nothing depends on a subscription or a datacentre.

## Layout

- Router/AP: consumer unit, stock firmware, no cloud account linked
- NAS: 2-bay mirror, holds photos, scans, the note archive, ISOs
- Pi: runs the internal git remote + a print queue; nothing else —
  resist scope creep

## Rules

- anything irreplaceable exists on the NAS AND an offline disk —
  see [backup routine](../areas/backup-routine.md)
- no IoT that phones home; the two smart plugs are on the guest VLAN
- if a service needs an account to function offline, it doesn't get
  installed — [cli cheat sheet](../resources/cli-cheat-sheet.md) has
  the firewall one-liners

## Open

- [ ] document the Pi image restore steps
- [ ] second offline disk to the parents' house for off-site
#tech #local
