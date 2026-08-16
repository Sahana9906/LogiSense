# ER Diagram — Canonical Schema + Phase 1

```
locations 1--* suppliers
locations 1--* customers
locations 1--* shipments (origin)
locations 1--* shipments (destination)

products  1--* orders
suppliers 1--* orders
customers 1--* orders

orders    1--1 shipments
shipments 1--1 incidents

incidents 1--* investigation_runs
investigation_runs 1--1 incident_intake
```

All foreign keys except `orders.source_order_id` / `shipments.order_id`
are optional, since not every canonical concept exists in every dataset.
