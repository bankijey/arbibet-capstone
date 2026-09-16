{#
  Render a stored timestamp in PLATFORM time (Europe/Berlin).

  Every timestamp in CORE is TIMESTAMP_TZ, which stores the OFFSET THE WRITER
  USED and is displayed in that offset forever -- the session TIMEZONE
  parameter does not touch it. Only TIMESTAMP_LTZ is rendered in session time.

  The rows were written while the connector's session timezone was still
  Snowflake's factory default, America/Los_Angeles, so they carry -07:00/-08:00
  offsets. The instants are correct; the wall clock printed nine hours early,
  and an evening Ligue 1 kick-off appeared as 11:45 on the deep-dive page.

  Casting to TIMESTAMP_LTZ re-renders the same instant in session time. Both
  sessions are pinned to Europe/Berlin -- `on-run-start` here, and
  `session_parameters` in `warehouse.connect()` -- so the zone lives in one
  place per component rather than being repeated as a literal in every model.

  Re-writing the tables would also work and is not worth it: settle alone is
  5.9M rows, and a stored offset is not wrong, only local to its writer.
#}
{% macro platform_time(column) -%}
    {{ column }}::timestamp_ltz
{%- endmacro %}
