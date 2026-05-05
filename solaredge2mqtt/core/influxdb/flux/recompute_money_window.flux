// Recompute the money_saved / money_consumed / money_delivered /
// money_price_in / money_price_out fields for the past
// {{LOOKBACK_HOURS}} hours by joining the existing `energy` measurement
// with the `prices` measurement that PriceProvider writes. Used for
// historical backfill — does not touch the underlying energy aggregation
// (so it works even when raw powerflow_raw retention has expired).

import "date"
import "join"

stopTime = date.truncate(t: now(), unit: 1h)
startTime = date.sub(from: stopTime, d: {{LOOKBACK_HOURS}}h)
bucket = "{{BUCKET_NAME}}"

energy = from(bucket: bucket)
    |> range(start: startTime, stop: stopTime)
    |> filter(fn: (r) => r._measurement == "energy")
    |> filter(fn: (r) =>
        r._field == "consumer_used_production"
            or r._field == "grid_delivery"
            or r._field == "grid_consumption")

prices_in = from(bucket: bucket)
    |> range(start: startTime, stop: stopTime)
    |> filter(fn: (r) => r._measurement == "prices" and r._field == "price_in")
    |> keep(columns: ["_time", "_value"])
    |> rename(columns: {_value: "price_in"})

prices_out = from(bucket: bucket)
    |> range(start: startTime, stop: stopTime)
    |> filter(fn: (r) => r._measurement == "prices" and r._field == "price_out")
    |> keep(columns: ["_time", "_value"])
    |> rename(columns: {_value: "price_out"})

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
