{#
  Render a stored timestamp in PLATFORM time (Europe/Berlin).

  In Snowflake this cast TIMESTAMP_TZ to TIMESTAMP_LTZ: TIMESTAMP_TZ kept the
  offset its writer used, and rows written under the session default printed
  nine hours early. DuckDB's TIMESTAMPTZ stores an instant and always renders
  it in the session zone, which on-run-start pins to Europe/Berlin -- so here
  the macro is the identity. It stays, so the models still say which columns
  are meant to be read in platform time.
#}
{% macro platform_time(column) -%}
    {{ column }}
{%- endmacro %}
