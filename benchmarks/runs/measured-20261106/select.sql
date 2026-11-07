\set id random(1, 10000)
SELECT id, payload FROM continuity WHERE id = :id;
