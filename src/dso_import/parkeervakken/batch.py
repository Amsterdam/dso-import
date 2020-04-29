import datetime
import logging
import pathlib
import re
from django.db import connection
from django.contrib.gis.gdal import DataSource
from django.utils.dateparse import parse_time
from schematools.contrib.django.models import Dataset
from dso_import.batch import batch

log = logging.getLogger(__name__)

# The way file names are expected to be.
stadsdeel_c = re.compile(
    r'^(?P<stadsdeel>[a-zA-Z-]*)_parkeerhaven.*_(?P<date>[0-9]{8})$')


class ImportParkeervakkenTask(batch.BasicTask):
    dataset = "parkeervakken"

    def __init__(self, models, *args, **kwargs):
        self.models = models
        self.source = pathlib.Path('/home/ngaranko/Projects/amsterdam/data/parkeervakken/')

    def _format_temp_table_name(self, model):
        return f"{model._meta.db_table}_temp"

    def before(self):
        """
        Create temporary tables.
        """
        cursor = connection.cursor()
        for model_name in ["parkeervakken", "parkeervakken_regimes"]:
            model = self.models[model_name]
            base_table = model._meta.db_table
            temp_table = self._format_temp_table_name(model)
            log.debug(f"Drop temporary table: {temp_table}")
            cursor.execute(f"DROP TABLE IF EXISTS {temp_table}")
            log.debug(f"Create temporary table: {temp_table} as {base_table}")
            cursor.execute(f"CREATE TABLE {temp_table} (LIKE {base_table} INCLUDING ALL)")
            if model._meta.model_name == "parkeervakken_regimes":
                log.debug(f"Create Sequence for `{temp_table}`)")
                cursor.execute(f"DROP SEQUENCE IF EXISTS {temp_table}_id_seq CASCADE")
                cursor.execute(f"CREATE SEQUENCE {temp_table}_id_seq OWNED BY {temp_table}.id")
                cursor.execute(f"ALTER TABLE {temp_table} ALTER COLUMN id SET DEFAULT NEXTVAL('{temp_table}_id_seq')")
            model._meta.original_db_table = base_table
            model._meta.db_table = temp_table

    def process(self):
        latest_date = self.find_latest_date(self.source, file_type='shp')
        log.debug("Import date: {}".format(latest_date))
        self.import_shape_data(self.source, latest_date)

    def after(self):
        """
        Check if data is valid and replace live tables with temporary tables.
        """
        # TODO: Data checks
        cursor = connection.cursor()
        for model_name in ["parkeervakken", "parkeervakken_regimes"]:
            model = self.models[model_name]
            base_table = model._meta.original_db_table
            temp_table = model._meta.db_table
            log.debug(f"Replace {base_table} with {temp_table}")
            cursor.execute(f"DROP TABLE IF EXISTS {base_table}")
            cursor.execute(f"ALTER TABLE {temp_table} RENAME TO {base_table}")
            model._meta.db_table = model._meta.original_db_table

    def find_latest_date(self, source, file_type):
        """
        :type source: pathlib.Path
        """
        last_date = None

        for path in source.glob('*.{}'.format(file_type)):
            match = stadsdeel_c.match(path.stem)
            _, date_string = match.groups()
            date = datetime.datetime.strptime(date_string, '%Y%m%d')

            if last_date is None or last_date < date:
                last_date = date

        return last_date

    def import_shape_data(self, source, latest_date):
        """Unzip all data in :param:`zip_file` and load the shape files.

        :type source: pathlib.Path
        :type latest_date: datetime.datetime
        :type conn: psycopg2.extensions.connection
        :type cur: psycopg2.extensions.connection
        """

        for shp_file in source.glob('*.shp'):
            match = stadsdeel_c.match(shp_file.stem)
            _, date_string = match.groups()
            file_date = datetime.datetime.strptime(date_string, '%Y%m%d')

            if latest_date != file_date:
                continue

            log.debug('Load %s', shp_file)

            count, count_created = self.load_shape_file(shp_file)
            log.debug(f"Done. Processed records: {count} where new {count_created}")

        log.debug("Completed.")

    def load_shape_file(self, shp_file):
        """Import data from shape files into the database. The shape files are
        imported by first creating an sql script using `shp2pgsql` and executing
        this on the database.

        :type conn: psycopg2.extensions.connection
        :type cur: psycopg2.extensions.connection
        :type shp_file: pathlib.Path
        :param shp_file: The path to the shape file.
        """

        data_source = DataSource(str(shp_file))

        if len(data_source) != 1:
            # Break hard, if multi layer file is provided.
            raise ValueError(f"Multi Layer shape file detected! {shp_file}")

        count_created = 0
        count = 0
        for row in data_source[0]:
            parkeervaak, created = self.create_parkeervaak(row)

            if any([row.get("KENTEKEN"),
                    row.get("BORD"),
                    row.get("E_TYPE"),
                    row.get("BEGINTIJD1"),
                    row.get("EINDTIJD1"),
                    row.get("BEGINTIJD2"),
                    row.get("EINDTIJD2"),
                    row.get("TVM_BEGINT"),
                    row.get("TVM_EINDT"),
                    row.get("TVM_BEGIND"),
                    row.get("TVM_EINDD"),
                    row.get("TVM_OPMERK")]):
                self.create_regimes(parkeervaak, row)

            count += 1
            if created:
                count_created += 1
        return count, count_created

    def create_parkeervaak(self, row):
        Parkeervaak = self.models["parkeervakken"]
        created = False
        try:
            parkeervaak = Parkeervaak.objects.get(pk=row.get("PARKEER_ID"))
        except Parkeervaak.DoesNotExist:
            created = True
            parkeervaak = Parkeervaak.objects.create(
                id=row.get("PARKEER_ID"),
                buurtcode=row.get("BUURTCODE"),
                straatnaam=row.get("STRAATNAAM"),
                soort=row.get("SOORT"),
                type=row.get("TYPE"),
                aantal=row.get("AANTAL"),
                geom=row.geom.wkt,
                e_type=row.get("E_TYPE"),
            )
        else:
            log.debug(f"DUPLICATE ID {parkeervaak.id}")
        return parkeervaak, created

    def create_regimes(self, parkeervaak, row):
        parkeervaak.regimes.all().delete()

        Regime = self.models["parkeervakken_regimes"]

        days = self.days_from_row(row)

        base_data = dict(
            soort=row.get("SOORT"),
            e_type=row.get("E_TYPE"),
            bord=row.get("BORD"),
            begin_tijd="00:00",
            eind_tijd="23:59",
            opmerking=row.get("OPMERKING"),
            dagen=days,
            parent=parkeervaak,
        )

        if row.get("KENTEKEN"):
            kenteken_regime = base_data.copy()
            kenteken_regime.update(dict(
                kenteken=row.get("KENTEKEN"),
                begin_tijd=self.format_time(row.get("BEGINTIJD1")),
                eind_tijd=self.format_time(row.get("EINDTIJD1"))
            ))
            Regime.objects.create(**kenteken_regime)
        elif any([row.get("BEGINTIJD1"), row.get("EINDTIJD1")]):
            Regime.objects.create(**base_data)

            second_mode = base_data.copy()
            second_mode["begin_tijd"] = self.format_time(row.get("BEGINTIJD1"))
            second_mode["eind_tijd"] = self.format_time(row.get("EINDTIJD1"))
            Regime.objects.create(**second_mode)
        elif any([row.get("BEGINTIJD2"), row.get("EINDTIJD2")]):
            Regime.objects.create(**base_data)

            second_mode = base_data.copy()
            second_mode["begin_tijd"] = self.format_time(row.get("BEGINTIJD2"))
            second_mode["eind_tijd"] = self.format_time(row.get("EINDTIJD2"))

            Regime.objects.create(**second_mode)
        elif any([
            row.get("TVM_BEGINT"),
            row.get("TVM_EINDT"),
            row.get("TVM_BEGIND"),
            row.get("TVM_EINDD"),
            row.get("TVM_OPMERK"),
        ]):
            Regime.objects.create(**base_data)
            # TVM
            tvm_mode = base_data.copy()
            tvm_mode.update(
                dict(
                    begin_datum=row.get("TVM_BEGIND"),
                    eind_datum=row.get("TVM_EINDD"),
                    opmerking=row.get("TVM_OPMERK"),
                    begin_tijd=self.format_time(row.get("TVM_BEGINT")),
                    eind_tijd=self.format_time(row.get("TVM_EINDT")),
                )
            )
            Regime.objects.create(**tvm_mode)

    def days_from_row(self, row):
        """
        Parse week days from row.
        """
        week_days = ["ma", "di", "wo", "do", "vr", "za", "zo"]

        if row.get("MA_VR"):
            # Monday to Friday
            days = week_days[:4]
        elif row.get("MA_ZA"):
            # Monday to Saturday
            days = week_days[:5]
        elif all([row.get(day.upper()) for day in week_days]):
            # All days apply
            days = week_days

        elif not any([row.get(day.upper()) for day in week_days]):
            # All days apply
            days = week_days
        else:
            # One day permit
            days = [day for day in week_days if row.get(day.upper()) is not None]

        return days

    def format_time(self, value, default=None):
        """
        Format time or return None
        """
        if value is not None:
            if value == "24:00":
                value = "23:59"
            if value.startswith("va "):
                value = value[3:]

            parsed = parse_time(value)
            if parsed is not None:
                return parsed.strftime("%H:%M")
        return default


class ImportParkeervakkenJob(batch.BasicJob):
    name = "Import Parkeervakken"

    def __init__(self, **kwargs):
        dataset = Dataset.objects.get(name="parkeervakken")
        self.models = {
            model._meta.model_name: model for model in dataset.create_models()
        }

    def tasks(self):
        return [ImportParkeervakkenTask(models=self.models)]
