# CH Logistics

Driver mobile/web app, live GPS tracking, trip management, manifest pickup &
delivery, FCM push, and dispatcher live fleet map.

Companion app to `ch_erp15` (umbrella) — owns the logistics domain.

## Phase 2 features (current)
- Real-time driver GPS location streaming (`CH Driver Location`)
- Google Maps driver self-view (own position + next stop)
- Google Maps live fleet view (all active drivers, color-coded by status)
- 2-photo manifest rejection (`CH Manifest Rejection`)
- Tracking settings (API key, ping cadence, retention)

## Phase 3+ (planned)
Migrate the following from `ch_erp15` (kept there for backward-compat shims):
- `CH Logistics Trip`, `CH Logistics Trip Stop`
- `CH Logistics Exception`, `CH Logistics Settings`
- `CH Transfer Manifest`, `CH Transfer Manifest Item`
- `CH Driver Device`, `Stock Entry Logistics History`
- Modules: `logistics_api`, `driver_api`, `driver_push`, `driver_status`,
  `transfer_manifest_api`, `buyback_logistics_bridge`
- Pages: `delivery_app`, `logistics_control_tower`
- Portal: `www/my-trips`

## Install
```bash
bench get-app /path/to/ch_logistics
bench --site <site> install-app ch_logistics
bench --site <site> migrate
```

## License
MIT
