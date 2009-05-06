"""
Oracle database backend for Django-Jython

Modified by: J. Juneau - juneau001@gmail.com
Modified Date:  01/10/2009

Modified On:  04/30/2009
    - Finalized for release with 1.0b2
"""

try:
    from com.ziclix.python.sql import zxJDBC as Database
except ImportError, e:
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured("Error loading zxJDBC module: %s" % e)

import os
from django.db.backends import *
from doj.backends.zxjdbc.oracle import query
from django.db.backends.oracle.client import DatabaseClient
from doj.backends.zxjdbc.oracle.creation import DatabaseCreation
from doj.backends.zxjdbc.oracle.introspection import DatabaseIntrospection
from django.utils.encoding import smart_str

from doj.backends.zxjdbc.common import zxJDBCOperationsMixin, zxJDBCFeaturesMixin
from doj.backends.zxjdbc.oracle.zxJDBCCursorWrapperOracle import zxJDBCCursorWrapperOracle

# Oracle takes client-side character set encoding from the environment.
os.environ['NLS_LANG'] = '.UTF8'
# This prevents unicode from getting mangled by getting encoded into the
# potentially non-unicode database character set.
os.environ['ORA_NCHAR_LITERAL_REPLACE'] = 'TRUE'

DatabaseError = Database.DatabaseError
IntegrityError = Database.IntegrityError


class DatabaseFeatures(zxJDBCFeaturesMixin, BaseDatabaseFeatures):
    empty_fetchmany_value = ()
    needs_datetime_string_cast = False
    uses_custom_query_class = True
    interprets_empty_strings_as_nulls = True

class DatabaseOperations(zxJDBCOperationsMixin, BaseDatabaseOperations):
    def autoinc_sql(self, table, column):
#        # To simulate auto-incrementing primary keys in Oracle, we have to
#        # create a sequence and a trigger.
        sq_name = get_sequence_name(table)
        tr_name = get_trigger_name(table)
        tbl_name = self.quote_name(table)
        col_name = self.quote_name(column)
        sequence_sql = """
            DECLARE
                i INTEGER;
            BEGIN
                SELECT COUNT(*) INTO i FROM USER_CATALOG
                   WHERE TABLE_NAME = '%(sq_name)s' AND TABLE_TYPE = 'SEQUENCE';
                IF i = 0 THEN
                    EXECUTE IMMEDIATE 'CREATE SEQUENCE %(sq_name)s';
                END IF;
            END;
            /""" % locals()
        trigger_sql = """
            CREATE OR REPLACE TRIGGER %(tr_name)s
            BEFORE INSERT ON %(tbl_name)s
            FOR EACH ROW
            WHEN (new.%(col_name)s IS NULL)
                BEGIN
                    SELECT %(sq_name)s.nextval
                    INTO :new.%(col_name)s FROM dual;
                END;
                /""" % locals()
        return sequence_sql, trigger_sql
    def date_extract_sql(self, lookup_type, field_name):
#        # http://download-east.oracle.com/docs/cd/B10501_01/server.920/a96540/functions42a.htm#1017163
        return "EXTRACT(%s FROM %s)" % (lookup_type, field_name)

    def date_trunc_sql(self, lookup_type, field_name):
        # Oracle uses TRUNC() for both dates and numbers.
        # http://download-east.oracle.com/docs/cd/B10501_01/server.920/a96540/functions155a.htm#SQLRF06151
        if lookup_type == 'day':
            sql = 'TRUNC(%s)' % field_name
        else:
            sql = "TRUNC(%s, '%s')" % (field_name, lookup_type)
        return sql

    def datetime_cast_sql(self):
        return "TO_TIMESTAMP(%s, 'YYYY-MM-DD HH24:MI:SS.FF')"

    def deferrable_sql(self):
        return " DEFERRABLE INITIALLY DEFERRED"

    def drop_sequence_sql(self, table):
        return "DROP SEQUENCE %s;" % self.quote_name(get_sequence_name(table))

    def field_cast_sql(self, db_type):
        if db_type and db_type.endswith('LOB'):
            return "DBMS_LOB.SUBSTR(%s)"
        else:
            return "%s"

    def last_insert_id(self, cursor, table_name, pk_name):
        sq_name = util.truncate_name(table_name, self.max_name_length() - 3)
        cursor.execute('SELECT %s_sq.currval FROM dual' % sq_name)
        return cursor.fetchone()[0]

    def lookup_cast(self, lookup_type):
        if lookup_type in ('iexact', 'icontains', 'istartswith', 'iendswith'):
            return "UPPER(%s)"
        return "%s"

    def max_name_length(self):
        return 30

    def prep_for_iexact_query(self, x):
        return x

    def query_class(self, DefaultQueryClass):
        return query.query_class(DefaultQueryClass, Database)

    def quote_name(self, name):
        # SQL92 requires delimited (quoted) names to be case-sensitive.  When
        # not quoted, Oracle has case-insensitive behavior for identifiers, but
        # always defaults to uppercase.
        # We simplify things by making Oracle identifiers always uppercase.
        if not name.startswith('"') and not name.endswith('"'):
            name = '"%s"' % util.truncate_name(name.upper(), self.max_name_length())
        return name.upper()

    def random_function_sql(self):
        return "DBMS_RANDOM.RANDOM"

    def regex_lookup_9(self, lookup_type):
        raise NotImplementedError("Regexes are not supported in Oracle before version 10g.")

    def regex_lookup_10(self, lookup_type):
        if lookup_type == 'regex':
            match_option = "'c'"
        else:
            match_option = "'i'"
        return 'REGEXP_LIKE(%%s, %%s, %s)' % match_option

    def regex_lookup(self, lookup_type):
        # If regex_lookup is called before it's been initialized, then create
        # a cursor to initialize it and recur.
        from django.db import connection
        connection.cursor()
        return connection.ops.regex_lookup(lookup_type)

    def sql_flush(self, style, tables, sequences):
        # Return a list of 'TRUNCATE x;', 'TRUNCATE y;',
        # 'TRUNCATE z;'... style SQL statements
        if tables:
            # Oracle does support TRUNCATE, but it seems to get us into
            # FK referential trouble, whereas DELETE FROM table works.
            sql = ['%s %s %s;' % \
                    (style.SQL_KEYWORD('DELETE'),
                     style.SQL_KEYWORD('FROM'),
                     style.SQL_FIELD(self.quote_name(table))
                     ) for table in tables]
            # Since we've just deleted all the rows, running our sequence
            # ALTER code will reset the sequence to 0.
            for sequence_info in sequences:
                sequence_name = get_sequence_name(sequence_info['table'])
                table_name = self.quote_name(sequence_info['table'])
                column_name = self.quote_name(sequence_info['column'] or 'id')
                query = _get_sequence_reset_sql() % {'sequence': sequence_name,
                                                     'table': table_name,
                                                     'column': column_name}
                sql.append(query)
            return sql
        else:
            return []

    def sequence_reset_sql(self, style, model_list):
        from django.db import models
        output = []
        query = _get_sequence_reset_sql()
        for model in model_list:
            for f in model._meta.local_fields:
                if isinstance(f, models.AutoField):
                    table_name = self.quote_name(model._meta.db_table)
                    sequence_name = get_sequence_name(model._meta.db_table)
                    column_name = self.quote_name(f.column)
                    output.append(query % {'sequence': sequence_name,
                                           'table': table_name,
                                           'column': column_name})
                    break # Only one AutoField is allowed per model, so don't bother continuing.
            for f in model._meta.many_to_many:
                table_name = self.quote_name(f.m2m_db_table())
                sequence_name = get_sequence_name(f.m2m_db_table())
                column_name = self.quote_name('id')
                output.append(query % {'sequence': sequence_name,
                                       'table': table_name,
                                       'column': column_name})
        return output

    def start_transaction_sql(self):
       return ''

    def tablespace_sql(self, tablespace, inline=False):
        return "%sTABLESPACE %s" % ((inline and "USING INDEX " or ""), self.quote_name(tablespace))

    def value_to_db_time(self, value):
        if value is None:
            return None
        if isinstance(value, basestring):
            return datetime.datetime(*(time.strptime(value, '%H:%M:%S')[:6]))
        return datetime.datetime(1900, 1, 1, value.hour, value.minute,
                                 value.second, value.microsecond)

    def year_lookup_bounds_for_date_field(self, value):
        first = '%s-01-01'
        second = '%s-12-31'
        return [first % value, second % value]

class DatabaseWrapper(BaseDatabaseWrapper):

    operators = {
        'exact': '= %s',
        'iexact': '= UPPER(%s)',
        'contains': "LIKEC %s ESCAPE '\\'",
        'icontains': "LIKEC UPPER(%s) ESCAPE '\\'",
        'gt': '> %s',
        'gte': '>= %s',
        'lt': '< %s',
        'lte': '<= %s',
        'startswith': "LIKEC %s ESCAPE '\\'",
        'endswith': "LIKEC %s ESCAPE '\\'",
        'istartswith': "LIKEC UPPER(%s) ESCAPE '\\'",
        'iendswith': "LIKEC UPPER(%s) ESCAPE '\\'",
    }
    oracle_version = None

    def __init__(self, *args, **kwargs):
        super(DatabaseWrapper, self).__init__(*args, **kwargs)

        self.features = DatabaseFeatures()
        self.ops = DatabaseOperations()
        self.client = DatabaseClient(self)
        self.creation = DatabaseCreation(self)
        self.introspection = DatabaseIntrospection(self)
        self.validation = BaseDatabaseValidation()

    def _valid_connection(self):
        return self.connection is not None

    def _cursor(self, settings):
        cursor = None
        if self.connection is None:
            #  Configure and connect to database using zxJDBC
            if settings.DATABASE_NAME == '':
                from django.core.exceptions import ImproperlyConfigured
                raise ImproperlyConfigured("You need to specify DATABASE_NAME in your Django settings file.")
            host = settings.DATABASE_HOST or 'localhost'
            port = settings.DATABASE_PORT or ''
            conn_string = "jdbc:oracle:thin:@%s:%s:%s" % (host, port,
                                                         settings.DATABASE_NAME)
            self.connection = Database.connect(
                    "jdbc:oracle:thin:@%s:%s:%s" % (host, port, settings.DATABASE_NAME), settings.DATABASE_USER, settings.DATABASE_PASSWORD,
                    "oracle.jdbc.OracleDriver")
            # make transactions transparent to all cursors
            # Commented out as set_default_isolation_level is not recognized via zxJDBC impl - JJ
            #set_default_isolation_level(self.connection)
            #cursor = self.connection
            cursor = CursorWrapper(self.connection.cursor())
            cursor.numberAsStrings = True
            # Set oracle date to ansi date format.  This only needs to execute
            # once when we create a new connection.
            cursor.execute("ALTER SESSION SET NLS_DATE_FORMAT = 'YYYY-MM-DD' "
                           "NLS_TIMESTAMP_FORMAT = 'YYYY-MM-DD HH24:MI:SS.FF'")

            try:
                self.connection.stmtcachesize = 20
            except:
                # Django docs specify cx_Oracle version 4.3.1 or higher, but
                # stmtcachesize is available only in 4.3.2 and up.
                pass
        if not cursor:
            cursor = CursorWrapper(self.connection.cursor())
        # Default arraysize of 1 is highly sub-optimal.
 #       cursor.arraysize = 100
        return cursor



CursorWrapper = zxJDBCCursorWrapperOracle


class OracleParam(object):
    """
    Wrapper object for formatting parameters for Oracle. If the string
    representation of the value is large enough (greater than 4000 characters)
    the input size needs to be set as NCLOB. Alternatively, if the parameter has
    an `input_size` attribute, then the value of the `input_size` attribute will
    be used instead. Otherwise, no input size will be set for the parameter when
    executing the query.
    """
    def __init__(self, param, charset, strings_only=False):
        self.smart_str = smart_str(param, charset, strings_only)
        if hasattr(param, 'input_size'):
            # If parameter has `input_size` attribute, use that.
            self.input_size = param.input_size
        elif isinstance(param, basestring) and len(param) > 4000:
            # Mark any string parameter greater than 4000 characters as an NCLOB.
            self.input_size = Database.NCLOB
        else:
            self.input_size = None

#  THE FOLLOWING IMPLEMENTATION IS NOT USED BY THE ORACLE BACKEND, IT ONLY
#  REMAINS INTACT FOR DOCUMENTATION PURPOSES, SEE zxJDBCCursorWrapperOracle.py
#  FOR CURRENT IMPLEMENTATION - JJ
class FormatStylePlaceholderCursor(CursorWrapper):
    """
    #Django uses "format" (e.g. '%s') style placeholders, but Oracle uses ":var"
    #style. This fixes it -- but note that if you want to use a literal "%s" in
    #a query, you'll need to use "%%s".

    We also do automatic conversion between Unicode on the Python side and
    UTF-8 -- for talking to Oracle -- in here.
    """
    charset = 'utf-8'


    def _format_params(self, params):
        if isinstance(params, dict):
            result = {}
            for key, value in params.items():
                result[smart_str(key, self.charset)] = OracleParam(param, self.charset)
            return result
        else:
            return tuple([OracleParam(p, self.charset, True) for p in params])

    def _guess_input_sizes(self, params_list):
        if isinstance(params_list[0], dict):
            sizes = {}
            iterators = [params.iteritems() for params in params_list]
        else:
            sizes = [None] * len(params_list[0])
            iterators = [enumerate(params) for params in params_list]
        for iterator in iterators:
            for key, value in iterator:
                if value.input_size: sizes[key] = value.input_size

    def _param_generator(self, params):
        if isinstance(params, dict):
            return dict([(k, p.smart_str) for k, p in params.iteritems()])
        else:
            return [p.smart_str for p in params]
            
    def execute(self, sql, params=()):
        paramlist = []
        paramcount = 0
        if len(params) > 0:
            sql = sql % (('?',) * len(params))
            if (sql.find("INSERT INTO \"AUTH_USER\"") != -1) or (sql.find("UPDATE \"AUTH_USER\"") != -1):
                for param in params:
                    if param == "":
                     
                        paramlist.append("name")
                    else:
                        paramlist.append(params[paramcount])
                    paramcount = paramcount + 1
            else:
                paramlist = params
        sql = sql.rstrip("/")
        if sql.find("DESC LIMIT %d") != -1:
            sql = sql.rstrip("DESC LIMIT %d")
        self.cursor.execute(sql.rstrip(";"), paramlist)
        
    def fetchone(self):
        row = self.execute(self)
        if row is None:
            return row
        return tuple([e for e in row])

    def fetchmany(self, size=None):
        if size is None:
            size = self.arraysize
        return tuple([tuple([e for e in r]) for r in self.execute(self, "")])

    def fetchall(self):
        return tuple([tuple([e for e in r]) for r in self.execute(self, "")])
        
def to_unicode(s):
    """
    Convert strings to Unicode objects (and return all other data types
    unchanged).
    """
    if isinstance(s, basestring):
       return force_unicode(s)
    return s

def _get_sequence_reset_sql():
    # TODO: colorize this SQL code with style.SQL_KEYWORD(), etc.
    return """
        DECLARE
            startvalue integer;
            cval integer;
        BEGIN
            LOCK TABLE %(table)s IN SHARE MODE;
            SELECT NVL(MAX(%(column)s), 0) INTO startvalue FROM %(table)s;
            SELECT %(sequence)s.nextval INTO cval FROM dual;
            cval := startvalue - cval;
            IF cval != 0 THEN
                EXECUTE IMMEDIATE 'ALTER SEQUENCE %(sequence)s MINVALUE 0 INCREMENT BY '||cval;
                SELECT %(sequence)s.nextval INTO cval FROM dual;
                EXECUTE IMMEDIATE 'ALTER SEQUENCE %(sequence)s INCREMENT BY 1';
            END IF;
            COMMIT;
        END;
        /"""

def get_sequence_name(table):
    name_length = DatabaseOperations().max_name_length() - 3
    return '%s_SQ' % util.truncate_name(table, name_length).upper()

def get_trigger_name(table):
    name_length = DatabaseOperations().max_name_length() - 3
    return '%s_TR' % util.truncate_name(table, name_length).upper()