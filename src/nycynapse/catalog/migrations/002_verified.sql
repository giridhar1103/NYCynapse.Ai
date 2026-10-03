ALTER TABLE catalog.objects DROP CONSTRAINT objects_kind_check;
ALTER TABLE catalog.objects ADD CONSTRAINT objects_kind_check CHECK (kind IN ('workspace',
    'model', 'dimension', 'time', 'measure', 'metric', 'relationship', 'period', 'place',
    'instruction', 'verified'));
