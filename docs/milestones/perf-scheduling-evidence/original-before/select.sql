\set id random(1, 10000)
SELECT payload FROM comparison WHERE id = :id;
