\set id random(1, 10000)
WITH item AS MATERIALIZED (SELECT id, payload FROM continuity WHERE id = :id) SELECT id, payload FROM item;
