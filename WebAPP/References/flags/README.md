# Country flags

SVG flags from the flag-icons project (https://github.com/lipis/flag-icons), MIT
licensed (see LICENSE). Vendored so MUIOGO works fully offline, like every other
reference asset.

Only the flags for countries in the OG-Core calibration catalogue are included.
When a new country calibration appears in the register, add its 4x3 SVG here
(lowercase ISO2 name, e.g. `ke.svg`) and map its ISO3 code in
`WebAPP/App/Controller/OGCore.js`. Unmapped countries fall back to a
Font Awesome flag icon automatically.
