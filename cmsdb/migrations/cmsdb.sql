--
-- PostgreSQL database dump
--

SET statement_timeout = 0;
SET lock_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SET check_function_bodies = false;
SET client_min_messages = warning;

--
-- Name: plpgsql; Type: EXTENSION; Schema: -; Owner:
--

CREATE EXTENSION IF NOT EXISTS plpgsql WITH SCHEMA pg_catalog;

--
-- Name: EXTENSION plpgsql; Type: COMMENT; Schema: -; Owner:
--

COMMENT ON EXTENSION plpgsql IS 'PL/pgSQL procedural language';

CREATE USER pgadmin WITH SUPERUSER;

SET search_path = public, pg_catalog;

SET default_tablespace = '';

SET default_with_oids = false;

--
-- Name: abstractintegration; Type: TABLE; Schema: public; Owner: pgadmin; Tablespace:
--

CREATE TABLE abstractintegration (
    _data jsonb,
    _type character varying(128) NOT NULL,
    created_time timestamp DEFAULT current_timestamp,
    updated_time timestamp DEFAULT current_timestamp
);


ALTER TABLE abstractintegration OWNER TO pgadmin;

--
-- Name: accountlimits; Type: TABLE; Schema: public; Owner: pgadmin; Tablespace:
--

CREATE TABLE accountlimits (
    _data jsonb,
    _type character varying(128) NOT NULL,
    created_time timestamp DEFAULT current_timestamp,
    updated_time timestamp DEFAULT current_timestamp
);


ALTER TABLE accountlimits OWNER TO pgadmin;

--
-- Name: brightcoveplayer; Type: TABLE; Schema: public; Owner: pgadmin; Tablespace:
--

CREATE TABLE brightcoveplayer (
    _data jsonb,
    _type character varying(128) NOT NULL,
    created_time timestamp DEFAULT current_timestamp,
    updated_time timestamp DEFAULT current_timestamp
);


ALTER TABLE brightcoveplayer OWNER TO pgadmin;

--
-- Name: abstractplatform; Type: TABLE; Schema: public; Owner: pgadmin; Tablespace:
--

CREATE TABLE abstractplatform (
    _data jsonb,
    _type character varying(128) NOT NULL,
    created_time timestamp DEFAULT current_timestamp,
    updated_time timestamp DEFAULT current_timestamp
);


ALTER TABLE abstractplatform OWNER TO pgadmin;

--
-- Name: billingplans; Type: TABLE; Schema: public; Owner: pgadmin; Tablespace:
--

CREATE TABLE billingplans (
    _data jsonb,
    _type character varying(128) NOT NULL,
    created_time timestamp DEFAULT current_timestamp,
    updated_time timestamp DEFAULT current_timestamp
);


ALTER TABLE billingplans OWNER TO pgadmin;

--
-- Name: cdnhostingmetadatalist; Type: TABLE; Schema: public; Owner: pgadmin; Tablespace:
--

CREATE TABLE cdnhostingmetadatalist (
    _data jsonb,
    _type character varying(128) NOT NULL,
    created_time timestamp DEFAULT current_timestamp,
    updated_time timestamp DEFAULT current_timestamp
);


ALTER TABLE cdnhostingmetadatalist OWNER TO pgadmin;

--
-- Name: clip; Type: TABLE; Schema: public; Owner: pgadmin; Tablespace:
--

CREATE TABLE clip (
    _data jsonb,
    _type character varying(128) NOT NULL,
    created_time timestamp DEFAULT current_timestamp,
    updated_time timestamp DEFAULT current_timestamp
);

ALTER TABLE clip OWNER to pgadmin;
 
--
-- Name: experimentstrategy; Type: TABLE; Schema: public; Owner: pgadmin; Tablespace:
--

CREATE TABLE experimentstrategy (
    _data jsonb,
    _type character varying(128) NOT NULL,
    created_time timestamp DEFAULT current_timestamp,
    updated_time timestamp DEFAULT current_timestamp
);

ALTER TABLE experimentstrategy OWNER TO pgadmin;

--
-- Name: feature; Type: TABLE; Schema: public; Owner: pgadmin; Tablespace:
--

CREATE TABLE feature (
    _data jsonb,
    _type character varying(128) NOT NULL,
    created_time timestamp DEFAULT current_timestamp,
    updated_time timestamp DEFAULT current_timestamp
);


ALTER TABLE feature OWNER TO pgadmin;

--
-- Name: neonapikey; Type: TABLE; Schema: public; Owner: pgadmin; Tablespace:
--

CREATE TABLE neonapikey (
    _data jsonb,
    _type character varying(128) NOT NULL,
    created_time timestamp DEFAULT current_timestamp,
    updated_time timestamp DEFAULT current_timestamp
);


ALTER TABLE neonapikey OWNER TO pgadmin;

--
-- Name: neonapirequest; Type: TABLE; Schema: public; Owner: pgadmin; Tablespace:
--

CREATE TABLE neonapirequest (
    _data jsonb,
    _type character varying(128) NOT NULL,
    created_time timestamp DEFAULT current_timestamp,
    updated_time timestamp DEFAULT current_timestamp
);


ALTER TABLE neonapirequest OWNER TO pgadmin;

--
-- Name: neonplatform; Type: TABLE; Schema: public; Owner: pgadmin; Tablespace:
--

CREATE TABLE neonplatform (
    _data jsonb,
    _type character varying(128) NOT NULL,
    created_time timestamp DEFAULT current_timestamp,
    updated_time timestamp DEFAULT current_timestamp
);


ALTER TABLE neonplatform OWNER TO pgadmin;

--
-- Name: neonuseraccount; Type: TABLE; Schema: public; Owner: pgadmin; Tablespace:
--

CREATE TABLE neonuseraccount (
    _data jsonb,
    _type character varying(128) NOT NULL,
    created_time timestamp DEFAULT current_timestamp,
    updated_time timestamp DEFAULT current_timestamp
);


ALTER TABLE neonuseraccount OWNER TO pgadmin;

--
-- Name: processingstrategy; Type: TABLE; Schema: public; Owner: pgadmin; Tablespace:
--

CREATE TABLE processingstrategy (
    _data jsonb,
    _type character varying(128) NOT NULL,
    created_time timestamp DEFAULT current_timestamp,
    updated_time timestamp DEFAULT current_timestamp
);


ALTER TABLE processingstrategy OWNER TO pgadmin;

--
-- Name: request; Type: TABLE; Schema: public; Owner: pgadmin; Tablespace:
-- here for backwards compatibility, NeonApiRequest._baseclass_name() == request
--

CREATE TABLE request (
    _data jsonb,
    _type character varying(128) NOT NULL,
    created_time timestamp DEFAULT current_timestamp,
    updated_time timestamp DEFAULT current_timestamp
);

ALTER TABLE request OWNER TO pgadmin;
--
-- Name: tag; Type: TABLE; Schema: public; Owner: pgadmin; Tablespace:
--


CREATE TABLE tag (
    _data jsonb,
    _type character varying(128) NOT NULL,
    created_time timestamp DEFAULT current_timestamp,
    updated_time timestamp DEFAULT current_timestamp
);

ALTER TABLE tag OWNER TO pgadmin;
--
-- Name: tag_clip; Type: TABLE; Schema: public; Owner: pgadmin; Tablespace:
--


CREATE TABLE tag_clip (
    tag_id character varying(128) NOT NULL,
    clip_id character varying(32) NOT NULL
);

ALTER TABLE tag_clip OWNER TO pgadmin;
--
-- Name: tag_thumbnail; Type: TABLE; Schema: public; Owner: pgadmin; Tablespace:
--


CREATE TABLE tag_thumbnail (
    tag_id character varying(128) NOT NULL,
    thumbnail_id character varying(1024) NOT NULL
);

ALTER TABLE tag_thumbnail OWNER TO pgadmin;


--
-- Name: thumbnail; Type: TABLE; Schema: public; Owner: pgadmin; Tablespace:
--

CREATE TABLE thumbnailmetadata (
    _data jsonb,
    _type character varying(128) NOT NULL,
    created_time timestamp DEFAULT current_timestamp,
    updated_time timestamp DEFAULT current_timestamp,
    features bytea DEFAULT NULL
);


ALTER TABLE thumbnailmetadata OWNER TO pgadmin;

--
-- Name: thumbnailservingurls; Type: TABLE; Schema: public; Owner: pgadmin; Tablespace:
--

CREATE TABLE thumbnailservingurls (
    _data jsonb,
    _type character varying(128) NOT NULL,
    created_time timestamp DEFAULT current_timestamp,
    updated_time timestamp DEFAULT current_timestamp
);


ALTER TABLE thumbnailservingurls OWNER TO pgadmin;

--
-- Name: thumbnailstatus; Type: TABLE; Schema: public; Owner: pgadmin; Tablespace:
--

CREATE TABLE thumbnailstatus (
    _data jsonb,
    _type character varying(128) NOT NULL,
    created_time timestamp DEFAULT current_timestamp,
    updated_time timestamp DEFAULT current_timestamp
);


ALTER TABLE thumbnailstatus OWNER TO pgadmin;

--
-- Name: trackeraccountidmapper; Type: TABLE; Schema: public; Owner: pgadmin; Tablespace:
--

CREATE TABLE trackeraccountidmapper (
    _data jsonb,
    _type character varying(128) NOT NULL,
    created_time timestamp DEFAULT current_timestamp,
    updated_time timestamp DEFAULT current_timestamp
);


ALTER TABLE trackeraccountidmapper OWNER TO pgadmin;

--
-- Name: users; Type: TABLE; Schema: public; Owner: pgadmin; Tablespace:
--  since user is a keyword in postgres, we use users here instead
--

CREATE TABLE users (
    _data jsonb,
    _type character varying(128) NOT NULL,
    created_time timestamp DEFAULT current_timestamp,
    updated_time timestamp DEFAULT current_timestamp
);


ALTER TABLE users OWNER TO pgadmin;

--
-- Name: verification; Type: TABLE; Schema: public; Owner: pgadmin; Tablespace:
--

CREATE TABLE verification (
    _data jsonb,
    _type character varying(128) NOT NULL,
    created_time timestamp DEFAULT current_timestamp,
    updated_time timestamp DEFAULT current_timestamp
);


ALTER TABLE verification OWNER TO pgadmin;

--
-- Name: videometadata; Type: TABLE; Schema: public; Owner: pgadmin; Tablespace:
--

CREATE TABLE videometadata (
    _data jsonb,
    _type character varying(128) NOT NULL,
    created_time timestamp DEFAULT current_timestamp,
    updated_time timestamp DEFAULT current_timestamp
);


ALTER TABLE videometadata OWNER TO pgadmin;

--
-- Name: videorendition; Type: TABLE; Schema: public; Owner: pgadmin; Tablespace:
--

CREATE TABLE videorendition (
    _data jsonb,
    _type character varying(128) NOT NULL,
    created_time timestamp DEFAULT current_timestamp,
    updated_time timestamp DEFAULT current_timestamp
);


ALTER TABLE videorendition OWNER TO pgadmin;

--
-- Name: videostatus; Type: TABLE; Schema: public; Owner: pgadmin; Tablespace:
--

CREATE TABLE videostatus (
    _data jsonb,
    _type character varying(128) NOT NULL,
    created_time timestamp DEFAULT current_timestamp,
    updated_time timestamp DEFAULT current_timestamp
);


ALTER TABLE videostatus OWNER TO pgadmin;

-- Data for Name: abstractintegration; Type: TABLE DATA; Schema: public; Owner: pgadmin
--

COPY abstractintegration (_data, _type) FROM stdin;
\.

-- Data for Name: abstractplatform; Type: TABLE DATA; Schema: public; Owner: pgadmin
--

COPY abstractplatform (_data, _type) FROM stdin;
\.

--
-- Data for Name: brightcoveplayer; Type: TABLE DATA; Schema: public; Owner: pgadmin
--

COPY brightcoveplayer (_data, _type) FROM stdin;
\.

-- Data for Name: billingplans; Type: TABLE DATA; Schema: public; Owner: pgadmin
--

COPY billingplans (_data, _type) FROM stdin;
{"key": "demo", "plan_type": "demo", "max_video_posts": 25, "seconds_to_refresh_video_posts":86400, "max_video_size":900.0, "max_image_posts": 1000, "seconds_to_refresh_image_posts":86400}	BillingPlans
{"key": "pro_monthly", "plan_type": "pro_monthly", "max_video_posts": 500, "seconds_to_refresh_video_posts":2592000, "max_video_size":3600.0, "max_image_posts": 1000, "seconds_to_refresh_image_posts":86400}	BillingPlans
{"key": "pro_yearly", "plan_type": "pro_yearly", "max_video_posts": 500, "seconds_to_refresh_video_posts":2592000, "max_video_size":3600.0, "max_image_posts": 1000, "seconds_to_refresh_image_posts":86400}	BillingPlans
{"key": "premeire", "plan_type": "premeire", "max_video_posts": 500000, "seconds_to_refresh_video_posts":1800, "max_video_size":500000.0, "max_image_posts": 1000, "seconds_to_refresh_image_posts":86400}	BillingPlans
\.

--
-- Data for Name: cdnhostingmetadatalist; Type: TABLE DATA; Schema: public; Owner: pgadmin
--

COPY cdnhostingmetadatalist (_data, _type) FROM stdin;
\.

--
-- Data for Name: experimentstrategy; Type: TABLE DATA; Schema: public; Owner: pgadmin
--

COPY experimentstrategy (_data, _type) FROM stdin;
\.

--
-- Data for Name: accountlimits; Type: TABLE DATA; Schema: public; Owner: pgadmin
--

COPY accountlimits (_data, _type) FROM stdin;
\.

--
-- Data for Name: neonapikey; Type: TABLE DATA; Schema: public; Owner: pgadmin
--

COPY neonapikey (_data, _type) FROM stdin;
\.


--
-- Data for Name: neonapirequest; Type: TABLE DATA; Schema: public; Owner: pgadmin
--

COPY neonapirequest (_data, _type) FROM stdin;
\.


--
-- Data for Name: neonuseraccount; Type: TABLE DATA; Schema: public; Owner: pgadmin
--

COPY neonuseraccount (_data, _type) FROM stdin;
\.

--
-- Data for Name: processingstrategy; Type: TABLE DATA; Schema: public; Owner: pgadmin
--

COPY processingstrategy (_data, _type) FROM stdin;
\.

--
-- Data for Name: request; Type: TABLE DATA; Schema: public; Owner: pgadmin
--

COPY request (_data, _type) FROM stdin;
\.

--
-- Data for Name: tag; Type: TABLE DATA; Schema: public; Owner: pgadmin
--

COPY tag (_data, _type) FROM stdin;
\.

--
-- Data for Name: thumbnailmetadata; Type: TABLE DATA; Schema: public; Owner: pgadmin
--

COPY thumbnailmetadata (_data, _type) FROM stdin;
\.


--
-- Data for Name: thumbnailservingurls; Type: TABLE DATA; Schema: public; Owner: pgadmin
--

COPY thumbnailservingurls (_data, _type) FROM stdin;
\.


--
-- Data for Name: thumbnailstatus; Type: TABLE DATA; Schema: public; Owner: pgadmin
--

COPY thumbnailstatus (_data, _type) FROM stdin;
\.

--
-- Data for Name: trackeraccountidmapper; Type: TABLE DATA; Schema: public; Owner: pgadmin
--

COPY trackeraccountidmapper (_data, _type) FROM stdin;
\.

--
-- Data for Name: users; Type: TABLE DATA; Schema: public; Owner: pgadmin
--

COPY users (_data, _type) FROM stdin;
\.


--
-- Data for Name: videometadata; Type: TABLE DATA; Schema: public; Owner: pgadmin
--

COPY videometadata (_data, _type) FROM stdin;
\.

--
-- Data for Name: verification; Type: TABLE DATA; Schema: public; Owner: pgadmin
--

COPY verification (_data, _type) FROM stdin;
\.

--
-- Data for Name: videostatus; Type: TABLE DATA; Schema: public; Owner: pgadmin
--

COPY videostatus (_data, _type) FROM stdin;
\.

-- Key Indexes
--  all tables have a unique key for them

CREATE UNIQUE INDEX abstractplatform_key ON abstractplatform USING btree (((_data ->> 'key'::text)));
CREATE UNIQUE INDEX abstractintegration_key ON abstractintegration USING btree (((_data ->> 'key'::text)));
CREATE UNIQUE INDEX brightcoveplayer_key ON brightcoveplayer USING btree (((_data ->> 'key'::text)));
CREATE UNIQUE INDEX cdnhostingmetadatalist_key ON cdnhostingmetadatalist USING btree (((_data ->> 'key'::text)));
CREATE UNIQUE INDEX clip_key ON clip USING btree (((_data ->> 'key'::text)));
CREATE UNIQUE INDEX experimentstrategy_key ON experimentstrategy USING btree (((_data ->> 'key'::text)));
CREATE UNIQUE INDEX feature_key ON feature USING btree (((_data ->> 'key'::text)));
CREATE UNIQUE INDEX accountlimits_key ON accountlimits USING btree (((_data ->> 'key'::text)));
CREATE UNIQUE INDEX billingplans_key ON billingplans USING btree (((_data ->> 'key'::text)));
CREATE UNIQUE INDEX neonapikey_key ON neonapikey USING btree (((_data ->> 'key'::text)));
CREATE UNIQUE INDEX neonapirequest_key ON neonapirequest USING btree (((_data ->> 'key'::text)));
CREATE UNIQUE INDEX neonuseraccount_key ON neonuseraccount USING btree (((_data ->> 'key'::text)));
CREATE UNIQUE INDEX processingstrategy_key ON processingstrategy USING btree (((_data ->> 'key'::text)));
CREATE UNIQUE INDEX request_key ON request USING btree (((_data ->> 'key'::text)));
CREATE UNIQUE INDEX tag_key ON tag USING btree (((_data ->> 'key'::text)));
CREATE UNIQUE INDEX tag_thumbnail_key ON tag_thumbnail (tag_id, thumbnail_id);
CREATE UNIQUE INDEX tag_clip_key ON tag_clip (tag_id, clip_id);
CREATE UNIQUE INDEX thumbnailmetadata_key ON thumbnailmetadata USING btree (((_data ->> 'key'::text)));
CREATE UNIQUE INDEX thumbnailservingurls_key ON thumbnailservingurls USING btree (((_data ->> 'key'::text)));
CREATE UNIQUE INDEX thumbnailstatus_key ON thumbnailstatus USING btree (((_data ->> 'key'::text)));
CREATE UNIQUE INDEX trackeraccountidmapper_key ON trackeraccountidmapper USING btree (((_data ->> 'key'::text)));
CREATE UNIQUE INDEX users_key ON users USING btree (((_data ->> 'key'::text)));
CREATE UNIQUE INDEX videometadata_key ON videometadata USING btree (((_data ->> 'key'::text)));
CREATE UNIQUE INDEX videorendition_key ON videorendition USING btree (((_data ->> 'key'::text)));
CREATE UNIQUE INDEX videostatus_key ON videostatus USING btree (((_data ->> 'key'::text)));
CREATE UNIQUE INDEX verification_key ON verification USING btree (((_data ->> 'key'::text)));

-- Time updated indexes
--  since we should be accessing the data in small chunks let's index these

CREATE INDEX neonapirequest_updated ON neonapirequest USING btree (((updated_time::timestamp)));
CREATE INDEX thumbnailmetadata_updated ON thumbnailmetadata USING btree (((updated_time::timestamp)));
CREATE INDEX videometadata_updated ON videometadata USING btree (((updated_time::timestamp)));

-- index this as a text field, dates with locale are immutable, and do not play nice due to locale
CREATE INDEX videometadata_publish_date ON videometadata USING btree (((_data ->> 'publish_date'::text)));

-- Other indexes
--   other places where indexed data makes sense

CREATE INDEX neonapirequest_integration_id ON neonapirequest USING btree (((_data ->> 'integration_id'::text)));
CREATE INDEX neonapirequest_job_id ON neonapirequest USING btree (((_data ->> 'job_id'::text)));
CREATE INDEX neonapirequest_video_id ON neonapirequest USING btree (((_data ->> 'video_id'::text)));
CREATE INDEX neonapirequest_video_title ON neonapirequest USING btree (((_data ->> 'video_title'::text)));
CREATE INDEX tag_account_id_index ON tag ((_data ->> 'account_id'::text));
CREATE INDEX thumbnail_id_tag_id ON tag_thumbnail (thumbnail_id, tag_id);
CREATE INDEX clip_id_tag_id ON tag_clip (clip_id, tag_id);
CREATE INDEX thumbnailmetadata_video_id ON thumbnailmetadata USING btree (((_data ->> 'video_id'::text)));
CREATE INDEX videometadata_job_id ON videometadata USING btree (((_data ->> 'job_id'::text)));
CREATE INDEX videometadata_integration_id ON videometadata USING btree (((_data ->> 'integration_id'::text)));
CREATE INDEX videometadata_thumbnail_ids ON videometadata USING gin (((_data->'thumbnail_ids')));
CREATE INDEX videorendition_clip_id ON videometadata USING btree (((_data ->> 'clip_id'::text)));
CREATE INDEX videorendition_video_id ON videometadata USING btree (((_data ->> 'video_id'::text)));

-- Text pattern ops indexes, for places we do a lot of text comps

CREATE INDEX videometadata_key_text_idx ON videometadata ((_data->>'key') text_pattern_ops);
CREATE INDEX thumbnailstatus_key_text_idx ON thumbnailstatus ((_data->>'key') text_pattern_ops);
CREATE INDEX videostatus_key_text_idx ON videostatus ((_data->>'key') text_pattern_ops);

--
-- Notify Trigger, notifies all listeners of changes
--
CREATE OR REPLACE FUNCTION tables_notify_func() RETURNS trigger as $$
DECLARE
  payload text;
BEGIN
    IF TG_OP = 'DELETE' THEN
    payload := row_to_json(tmp)::text FROM (
            SELECT
                OLD._data->>'key' AS _key,
                TG_OP
        ) tmp;
    ELSE
        payload := row_to_json(tmp)::text FROM (
            SELECT
                NEW._data->>'key' AS _key,
                TG_OP
        ) tmp;
    END IF;

  PERFORM pg_notify(TG_TABLE_NAME::text, payload);
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

--
-- Update updated_time trigger, auto updates updated_time on UPDATE
--
CREATE OR REPLACE FUNCTION update_updated_time_column() RETURNS trigger as $$
BEGIN
    NEW.updated_time = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

CREATE TRIGGER abstractplatform_notify_trig
AFTER INSERT OR UPDATE OR DELETE
ON abstractplatform
FOR EACH ROW EXECUTE PROCEDURE tables_notify_func();

CREATE TRIGGER abstractplatform_update_updated_time_trig
BEFORE UPDATE
ON abstractplatform
FOR EACH ROW EXECUTE PROCEDURE update_updated_time_column();

CREATE TRIGGER abstractintegration_notify_trig
AFTER INSERT OR UPDATE OR DELETE
ON abstractintegration
FOR EACH ROW EXECUTE PROCEDURE tables_notify_func();

CREATE TRIGGER abstractintegration_update_updated_time_trig
BEFORE UPDATE
ON abstractintegration
FOR EACH ROW EXECUTE PROCEDURE update_updated_time_column();

CREATE TRIGGER brightcoveplayer_update_updated_time_trig
BEFORE UPDATE
ON brightcoveplayer
FOR EACH ROW EXECUTE PROCEDURE update_updated_time_column();

CREATE TRIGGER billingplans_update_updated_time_trig
BEFORE UPDATE
ON billingplans
FOR EACH ROW EXECUTE PROCEDURE update_updated_time_column();

CREATE TRIGGER cdnhostingmetadatalist_notify_trig
AFTER INSERT OR UPDATE OR DELETE
ON cdnhostingmetadatalist
FOR EACH ROW EXECUTE PROCEDURE tables_notify_func();

CREATE TRIGGER cdnhostingmetadatalist_update_updated_time_trig
BEFORE UPDATE
ON cdnhostingmetadatalist
FOR EACH ROW EXECUTE PROCEDURE update_updated_time_column();

CREATE TRIGGER experimentstrategy_notify_trig
AFTER INSERT OR UPDATE OR DELETE
ON experimentstrategy
FOR EACH ROW EXECUTE PROCEDURE tables_notify_func();

CREATE TRIGGER experimentstrategy_update_updated_time_trig
BEFORE UPDATE
ON experimentstrategy
FOR EACH ROW EXECUTE PROCEDURE update_updated_time_column();

CREATE TRIGGER feature_update_updated_time_trig
BEFORE UPDATE
ON feature
FOR EACH ROW EXECUTE PROCEDURE update_updated_time_column();

CREATE TRIGGER accountlimits_update_updated_time_trig
BEFORE UPDATE
ON accountlimits
FOR EACH ROW EXECUTE PROCEDURE update_updated_time_column();

CREATE TRIGGER neonapikey_notify_trig
AFTER INSERT OR UPDATE OR DELETE
ON neonapikey
FOR EACH ROW EXECUTE PROCEDURE tables_notify_func();

CREATE TRIGGER neonapikey_update_updated_time_trig
BEFORE UPDATE
ON neonapikey
FOR EACH ROW EXECUTE PROCEDURE update_updated_time_column();

CREATE TRIGGER neonapirequest_notify_trig
AFTER INSERT OR UPDATE OR DELETE
ON neonapirequest
FOR EACH ROW EXECUTE PROCEDURE tables_notify_func();

CREATE TRIGGER neonapirequest_update_updated_time_trig
BEFORE UPDATE
ON neonapirequest
FOR EACH ROW EXECUTE PROCEDURE update_updated_time_column();

CREATE TRIGGER neonplatform_notify_trig
AFTER INSERT OR UPDATE OR DELETE
ON neonplatform
FOR EACH ROW EXECUTE PROCEDURE tables_notify_func();

CREATE TRIGGER neonplatform_update_updated_time_trig
BEFORE UPDATE
ON neonplatform
FOR EACH ROW EXECUTE PROCEDURE update_updated_time_column();

CREATE TRIGGER neonuseraccount_notify_trig
AFTER INSERT OR UPDATE OR DELETE
ON neonuseraccount
FOR EACH ROW EXECUTE PROCEDURE tables_notify_func();

CREATE TRIGGER neonuseraccount_update_updated_time_trig
BEFORE UPDATE
ON neonuseraccount
FOR EACH ROW EXECUTE PROCEDURE update_updated_time_column();

CREATE TRIGGER processingstrategy_notify_trig
AFTER INSERT OR UPDATE OR DELETE
ON processingstrategy
FOR EACH ROW EXECUTE PROCEDURE tables_notify_func();

CREATE TRIGGER processingstrategy_update_updated_time_trig
BEFORE UPDATE
ON processingstrategy
FOR EACH ROW EXECUTE PROCEDURE update_updated_time_column();

CREATE TRIGGER request_notify_trig
AFTER INSERT OR UPDATE OR DELETE
ON request
FOR EACH ROW EXECUTE PROCEDURE tables_notify_func();

CREATE TRIGGER request_update_updated_time_trig
BEFORE UPDATE
ON request
FOR EACH ROW EXECUTE PROCEDURE update_updated_time_column();

CREATE TRIGGER tag_update_updated_time_trig
BEFORE UPDATE
ON tag
FOR EACH ROW EXECUTE PROCEDURE update_updated_time_column();

CREATE TRIGGER thumbnailmetadata_notify_trig
AFTER INSERT OR UPDATE OR DELETE
ON thumbnailmetadata
FOR EACH ROW EXECUTE PROCEDURE tables_notify_func();

CREATE TRIGGER thumbnailmetadata_update_updated_time_trig
BEFORE UPDATE
ON thumbnailmetadata
FOR EACH ROW EXECUTE PROCEDURE update_updated_time_column();

CREATE TRIGGER thumbnailservingurls_notify_trig
AFTER INSERT OR UPDATE OR DELETE
ON thumbnailservingurls
FOR EACH ROW EXECUTE PROCEDURE tables_notify_func();

CREATE TRIGGER thumbnailservingurls_update_updated_time_trig
BEFORE UPDATE
ON thumbnailservingurls
FOR EACH ROW EXECUTE PROCEDURE update_updated_time_column();

CREATE TRIGGER thumbnailstatus_notify_trig
AFTER INSERT OR UPDATE OR DELETE
ON thumbnailstatus
FOR EACH ROW EXECUTE PROCEDURE tables_notify_func();

CREATE TRIGGER thumbnailstatus_update_updated_time_trig
BEFORE UPDATE
ON thumbnailstatus
FOR EACH ROW EXECUTE PROCEDURE update_updated_time_column();

CREATE TRIGGER trackeraccountidmapper_notify_trig
AFTER INSERT OR UPDATE OR DELETE
ON trackeraccountidmapper
FOR EACH ROW EXECUTE PROCEDURE tables_notify_func();

CREATE TRIGGER trackeraccountidmapper_update_updated_time_trig
BEFORE UPDATE
ON trackeraccountidmapper
FOR EACH ROW EXECUTE PROCEDURE update_updated_time_column();

CREATE TRIGGER users_notify_trig
AFTER INSERT OR UPDATE OR DELETE
ON users
FOR EACH ROW EXECUTE PROCEDURE tables_notify_func();

CREATE TRIGGER users_update_updated_time_trig
BEFORE UPDATE
ON users
FOR EACH ROW EXECUTE PROCEDURE update_updated_time_column();

CREATE TRIGGER verification_update_updated_time_trig
BEFORE UPDATE
ON verification
FOR EACH ROW EXECUTE PROCEDURE update_updated_time_column();

CREATE TRIGGER videometadata_notify_trig
AFTER INSERT OR UPDATE OR DELETE
ON videometadata
FOR EACH ROW EXECUTE PROCEDURE tables_notify_func();

CREATE TRIGGER videometadata_update_updated_time_trig
BEFORE UPDATE
ON videometadata
FOR EACH ROW EXECUTE PROCEDURE update_updated_time_column();

CREATE TRIGGER videostatus_notify_trig
AFTER INSERT OR UPDATE OR DELETE
ON videostatus
FOR EACH ROW EXECUTE PROCEDURE tables_notify_func();

CREATE TRIGGER videostatus_update_updated_time_trig
BEFORE UPDATE
ON videostatus
FOR EACH ROW EXECUTE PROCEDURE update_updated_time_column();

REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA public FROM pgadmin;
GRANT ALL ON SCHEMA public TO pgadmin;
GRANT ALL ON SCHEMA public TO PUBLIC;

--
-- PostgreSQL database dump complete
--
