BEGIN;
    ALTER TABLE participants    ADD COLUMN anonymous_supporters boolean NOT NULL DEFAULT TRUE;
    ALTER TABLE tips            ADD COLUMN is_anonymous         boolean NOT NULL DEFAULT TRUE;
    ALTER TABLE transfers       ADD COLUMN is_anonymous         boolean NOT NULL DEFAULT TRUE;
END;
