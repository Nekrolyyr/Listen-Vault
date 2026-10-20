-- The research areas become the 26 OpenAlex fields (listen/areas.py). Existing placements move to the closest new field;
-- manual placements (pinned) keep their meaning this way too. The library job 'areas_all' places every paper again.

CREATE TEMP TABLE area_map (old_id text PRIMARY KEY, new_id text NOT NULL);
INSERT INTO area_map (old_id, new_id) VALUES
    ('social_media', 'social'), ('politics', 'social'), ('movements', 'social'), ('news', 'social'),
    ('misinformation', 'social'), ('culture', 'social'), ('digital_methods', 'computer_science'),
    ('technology', 'computer_science'), ('images', 'arts'), ('health_env', 'environment'), ('other', 'other');

UPDATE document_areas a SET primary_area = m.new_id FROM area_map m WHERE a.primary_area = m.old_id;

UPDATE document_areas a SET also_areas = ARRAY(
    SELECT DISTINCT m.new_id FROM unnest(a.also_areas) AS x JOIN area_map m ON m.old_id = x
    WHERE m.new_id NOT IN (a.primary_area, 'other'));

UPDATE topics t SET area = m.new_id FROM area_map m WHERE t.area = m.old_id;
