// Aggregate hourly powerflow + battery data and apply per-hour energy prices
// from the `prices` measurement that PriceProvider writes. When a given hour
// has no `prices` row we fall back to the configured static defaults so that
// money fields stay continuous.

import "array"
import "date"
import "join"

stopTime = date.truncate(t: now(), unit: 1h)
startTime = date.sub(from: stopTime, d: 2h)

// join.left errors with "cannot join on an empty table" when its right side
// has zero rows (e.g. before the day's prices have been fetched). Anchor the
// price tables with a sentinel row outside the energy window so the right
// side is never empty; the `if exists` guards below still fall back to the
// configured default for any energy hour without a real price match.
anchorTime = date.sub(from: startTime, d: 1h)

bucket = "{{BUCKET_NAME}}"

exclude = ["inverter_power", "grid_power", "battery_power"]

power =
    from(bucket: bucket)
        |> range(start: startTime)
        |> filter(fn: (r) => r._measurement == "powerflow_raw")
        |> filter(fn: (r) => not contains(value: r._field, set: exclude))
        |> set(key: "_measurement", value: "powerflow")

power
    |> aggregateWindow(every: 1h, fn: max, createEmpty: false)
    |> set(key: "agg_type", value: "max")
    |> map(fn: (r) => ({r with _time: date.truncate(t: date.sub(from: r._time, d: 1s), unit: 1h)}))
    |> to(bucket: bucket)

power
    |> aggregateWindow(every: 1h, fn: min, createEmpty: false)
    |> set(key: "agg_type", value: "min")
    |> map(fn: (r) => ({r with _time: date.truncate(t: date.sub(from: r._time, d: 1s), unit: 1h)}))
    |> to(bucket: bucket)

power
    |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
    |> set(key: "agg_type", value: "mean")
    |> map(fn: (r) => ({r with _time: date.truncate(t: date.sub(from: r._time, d: 1s), unit: 1h)}))
    |> to(bucket: bucket)

energy = power
    |> aggregateWindow(
        every: 1h,
        fn: (tables=<-, column) =>
            tables
                |> integral(unit: 1h)
                |> map(fn: (r) => ({r with _value: r._value / 1000.0})),
    )
    |> set(key: "_measurement", value: "energy")
    |> map(fn: (r) => ({r with _time: date.truncate(t: date.sub(from: r._time, d: 1s), unit: 1h)}))
    |> to(bucket: bucket)

prices_in =
    union(tables: [
        from(bucket: bucket)
            |> range(start: startTime)
            |> filter(fn: (r) => r._measurement == "prices" and r._field == "price_in")
            |> keep(columns: ["_time", "_value"])
            |> rename(columns: {_value: "price_in"}),
        array.from(rows: [{_time: anchorTime, price_in: {{PRICE_IN_DEFAULT}}}]),
    ])

prices_out =
    union(tables: [
        from(bucket: bucket)
            |> range(start: startTime)
            |> filter(fn: (r) => r._measurement == "prices" and r._field == "price_out")
            |> keep(columns: ["_time", "_value"])
            |> rename(columns: {_value: "price_out"}),
        array.from(rows: [{_time: anchorTime, price_out: {{PRICE_OUT_DEFAULT}}}]),
    ])

energy_with_in = join.left(
    left: energy,
    right: prices_in,
    on: (l, r) => l._time == r._time,
    as: (l, r) => ({l with price_in: if exists r.price_in then r.price_in else {{PRICE_IN_DEFAULT}}}),
)

energy_with_out = join.left(
    left: energy,
    right: prices_out,
    on: (l, r) => l._time == r._time,
    as: (l, r) => ({l with price_out: if exists r.price_out then r.price_out else {{PRICE_OUT_DEFAULT}}}),
)

energy_with_in
    |> filter(fn: (r) => r._field == "consumer_used_production")
    |> map(fn: (r) => ({r with _value: r._value * r.price_in}))
    |> set(key: "_field", value: "money_saved")
    |> drop(columns: ["price_in"])
    |> to(bucket: bucket)

energy_with_in
    |> filter(fn: (r) => r._field == "consumer_used_production")
    |> map(fn: (r) => ({r with _value: r.price_in}))
    |> set(key: "_field", value: "money_price_in")
    |> drop(columns: ["price_in"])
    |> to(bucket: bucket)

energy_with_out
    |> filter(fn: (r) => r._field == "grid_delivery")
    |> map(fn: (r) => ({r with _value: r._value * r.price_out}))
    |> set(key: "_field", value: "money_delivered")
    |> drop(columns: ["price_out"])
    |> to(bucket: bucket)

energy_with_out
    |> filter(fn: (r) => r._field == "grid_delivery")
    |> map(fn: (r) => ({r with _value: r.price_out}))
    |> set(key: "_field", value: "money_price_out")
    |> drop(columns: ["price_out"])
    |> to(bucket: bucket)

energy_with_in
    |> filter(fn: (r) => r._field == "grid_consumption")
    |> map(fn: (r) => ({r with _value: r._value * r.price_in}))
    |> set(key: "_field", value: "money_consumed")
    |> drop(columns: ["price_in"])
    |> to(bucket: bucket)

battery =
    from(bucket: bucket)
        |> range(start: startTime)
        |> filter(fn: (r) => r._measurement == "battery_raw")
        |> set(key: "_measurement", value: "battery")

battery
    |> aggregateWindow(every: 1h, fn: max, createEmpty: false)
    |> set(key: "agg_type", value: "max")
    |> map(fn: (r) => ({r with _time: date.truncate(t: date.sub(from: r._time, d: 1s), unit: 1h)}))
    |> to(bucket: bucket)

battery
    |> aggregateWindow(every: 1h, fn: min, createEmpty: false)
    |> set(key: "agg_type", value: "min")
    |> map(fn: (r) => ({r with _time: date.truncate(t: date.sub(from: r._time, d: 1s), unit: 1h)}))
    |> to(bucket: bucket)

battery
    |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
    |> set(key: "agg_type", value: "mean")
    |> map(fn: (r) => ({r with _time: date.truncate(t: date.sub(from: r._time, d: 1s), unit: 1h)}))
    |> to(bucket: bucket)
