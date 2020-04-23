import logging
import os
from collections import defaultdict
from itertools import islice

import sqlparse

from django.db import connection, transaction
from dso_import import settings
from dso_import.batch import batch, csv, geo
from dso_import.batch.objectstore import download_file
from dso_api.datasets.models import Dataset

GOB_SHAPE_ENCODING = "utf-8"

log = logging.getLogger(__name__)


def create_id(identificatie, volgnummer):
    return f"{identificatie}_{volgnummer:03}" if identificatie else None


def create_ids(row, naam_identificatie, naam_volgnummer):
    identificaties = row[naam_identificatie] or None
    result = []
    if identificaties:
        identificaties = identificaties.split("|")
        volgnummers = row[naam_volgnummer].split("|")
        for i in range(len(identificaties)):
            result.append(create_id(identificaties[i], int(volgnummers[i])))
    return result


def int_or_none(value):
    if value and value.isdigit():
        return int(value)
    else:
        return None


class ImportBagHTask(batch.BasicTask):
    dataset = "bagh"

    def __init__(self, *args, **kwargs):
        self.table = f"{self.__class__.dataset}_{self.__class__.name}"
        self.temp_table = f"{self.__class__.dataset}_temp"
        self.path = kwargs.get("path")
        self.models = kwargs["models"]
        self.model = self.models[self.__class__.name]
        self.gob_path = kwargs.get("gob_path", "bag")
        self.gob_id = {"bag": "BAG", "gebieden": "GBD"}[self.gob_path]

        self.filename = f"{self.gob_id}_{self.__class__.name}_ActueelEnHistorie.csv"
        self.source_path = f"{self.gob_path}/CSV_ActueelEnHistorie"
        self.reference_models = {
            model_name: set() for model_name in kwargs.get("references", [])
        }
        self.geotype = kwargs.get("geotype", "multipolygon")
        self.extra_fields = kwargs.get("extra_fields")
        self.count_no_ref = 0

    def get_non_pk_fields(self):
        return [x.attname for x in self.model._meta.get_fields() if not x.primary_key]

    def before(self):
        cursor = connection.cursor()
        cursor.execute(f"DROP TABLE IF EXISTS {self.temp_table}")
        cursor.execute(
            f"CREATE TEMPORARY TABLE {self.temp_table} AS TABLE {self.table} WITH NO DATA"
        )
        self.model._meta.db_table = self.temp_table

        if self.path:
            download_file(os.path.join(self.source_path, self.filename))

        for model_name in self.reference_models.keys():
            # cursor.execute(f"SELECT id from {self.models[model_name]._meta.db_table}")
            # self.reference_models[model_name] = set(chain.from_iterable(cursor.fetchall()))
            self.reference_models[model_name] = set(
                self.models[model_name].objects.values_list("id", flat=True)
            )
        cursor.close()

    def after(self):
        cursor = connection.cursor()
        cursor.execute(f"ALTER TABLE {self.temp_table} ADD PRIMARY KEY(id)")
        cursor.execute(f"CREATE INDEX ON {self.temp_table}(identificatie)")

        fail = False
        if self.do_date_checks() > 0:
            log.error(f"Data invalid. Skip table {self.table}")
            fail = True

        # validate_geometry(models.Stadsdeel)

        # Check rows to delete. In history database there should be no rows to delete
        cursor.execute(
            f"""
            SELECT COUNT(e.*) FROM {self.table} e
            LEFT JOIN  {self.temp_table} t ON e.id = t.id
            WHERE t.id IS NULL
            """
        )
        (count,) = cursor.fetchone()
        if count > 0:
            log.error(f"Rows deleted. Data invalid. Skip table {self.table}")
            fail = True
        if fail:
            raise ValueError("Stopped import. Do not continue because of errors")
        with transaction.atomic():
            cursor.execute(
                f"""
                INSERT INTO  {self.table}
                SELECT t.* FROM {self.temp_table} t
                LEFT JOIN  {self.table} e ON t.id = e.id
                WHERE e.id IS NULL
                """
            )
            log.info(f"Inserted into {self.table} : {cursor.rowcount}")
            setters = map(lambda x: f"{x} = t.{x}", self.get_non_pk_fields())
            # No
            cursor.execute(
                f"""
                UPDATE {self.table} e SET {",".join(setters)}
                FROM {self.temp_table} t
                WHERE e.id = t.id AND t IS DISTINCT FROM e
                """
            )
            log.info(f"Updated {self.table} : {cursor.rowcount}")

        cursor.execute(f"DROP TABLE {self.temp_table}")
        self.model._meta.db_table = self.table
        self.reference_models.clear()
        cursor.close()
        if self.count_no_ref:
            log.info(f"Skipped no valid reference: {self.count_no_ref}")

    def process(self):
        entries = csv.process_csv(self.path, self.filename, self.process_row)
        while True:
            slice = list(islice(entries, batch.BATCH_SIZE))
            if not slice:
                break
            self.model.objects.bulk_create(slice)

    def process_row(self, r):
        values = self.process_row_common(r)
        if values:
            return self.model(**values)
        else:
            return None

    def process_row_common(self, r):  # noqa: C901
        identificatie = r["identificatie"]
        volgnummer = int(r["volgnummer"])
        id1 = create_id(identificatie, volgnummer)
        begin_geldigheid = csv.parse_date(r["beginGeldigheid"])
        eind_geldigheid = csv.parse_date(r["eindGeldigheid"]) or None
        if not csv.is_valid_date_range(begin_geldigheid, eind_geldigheid):
            log.error(
                f"{self.name.title()} {id1} has invalid geldigheid {begin_geldigheid} {eind_geldigheid}; skipping"  # noqa: E501
            )
            return None

        values = {
            "id": id1,
            "identificatie": identificatie,
            "volgnummer": volgnummer,
            "begin_geldigheid": begin_geldigheid,
            "eind_geldigheid": eind_geldigheid,
            "registratiedatum": csv.parse_date_time(r["registratiedatum"]),
        }

        if "geometrie" in r:
            wkt_geometrie = r["geometrie"]
            if wkt_geometrie:
                geometrie = geo.get_geotype(wkt_geometrie, self.geotype)
                if not geometrie:
                    log.error(
                        f"{self.name.title()} {id1} has no valid geometry; skipping"
                    )
                    return None
            else:
                if eind_geldigheid is None:
                    # Only log when is is the current entity
                    log.warning(f"{self.name.title()} {id1} has no geometry")
                geometrie = None
            values["geometrie"] = geometrie

        if "naam" in r:
            values["naam"] = r["naam"]
        if "code" in r:
            values["code"] = r["code"]
        if "documentdatum" in r:
            values["documentdatum"] = csv.parse_date(r["documentdatum"])
            values["documentnummer"] = r["documentnummer"]

        if "aanduidingInOnderzoek" in r:
            values["aanduiding_in_onderzoek"] = csv.parse_yesno_boolean(
                r["aanduidingInOnderzoek"]
            )
        if "geconstateerd" in r:
            values["geconstateerd"] = csv.parse_yesno_boolean(r["geconstateerd"])
        if "status" in r:
            values["status"] = r["status"]
        if "type" in r:
            values["type"] = r["type"]

        if self.extra_fields:
            for k, l in self.extra_fields.items():
                values[k] = l(r)

        model_field_map = {
            "gemeente": "ligtIn:BRK.GME",
            "stadsdeel": "ligtIn:GBD.SDL",
            "ggw_gebied": "ligtIn:GBD.GGW",
            "wijk": "ligtIn:GBD.WIJK",
            "buurt": "ligtIn:GBD.BRT",
            "woonplaats": "ligtIn:BAG.WPS",
            "openbare_ruimte": "ligtAan:BAG.ORE",
            "ligplaats": "adresseert:BAG.LPS",
            "standplaats": "adresseert:BAG.SPS",
            "verblijfsobject": "adresseert:BAG.VOT",
        }
        for model_name in self.reference_models.keys():
            fname = model_field_map[model_name]
            identificatie = r[f"{fname}.identificatie"]
            volgnummer = r[f"{fname}.volgnummer"] or "1"
            id_rel = create_id(identificatie, int(volgnummer))
            if id_rel and id_rel not in self.reference_models[model_name]:
                log.error(
                    f"{self.name.title()} {id1} has invalid id {id_rel} for {model_name} ; skipping"
                )
                self.count_no_ref += 1
                return None
            else:
                values[f"{model_name}_id"] = id_rel
        self.log_progress()
        return values

    def do_date_checks(self):
        cursor = connection.cursor()
        cursor.execute(
            f"""
        SELECT identificatie, count(*)
        FROM {self.temp_table}
        WHERE eind_geldigheid IS NULL
        GROUP BY identificatie HAVING count(*) > 1
        """
        )
        multiple_endranges = cursor.fetchall()
        if len(multiple_endranges) > 0:
            log.error(f"Multiple open eind_geldigheid for: {multiple_endranges}")
            return 1

        # When checking for overlapping ranges we do not check for start_dates that are
        # the same because that happens quite often
        # the same because that happens quite often
        cursor.execute(
            f"""SELECT w1.id, w2.id FROM {self.temp_table} w1
        JOIN {self.temp_table} w2 ON w1.identificatie = w2.identificatie
        WHERE w1.volgnummer <> w2.volgnummer
        AND  w1.begin_geldigheid > w2.begin_geldigheid
        AND  (w1.begin_geldigheid < w2.eind_geldigheid OR w2.eind_geldigheid IS NULL)
        """
        )
        overlapping_ranges = cursor.fetchall()
        if len(overlapping_ranges) > 0:
            log.error(f"Overlapping date ranges for: {overlapping_ranges}")
            # For now only notify
            return 0
        return 0


class CreateBagHTables(batch.BasicTask):
    name = "create_tables"

    def process(self):
        processed = 0
        with open("dso_import/bagh/bagh_create.sql", "r") as sql_file:
            with connection.cursor() as c:
                for sql in sqlparse.split(sql_file.read()):
                    if sql and not sql.isspace():
                        c.execute(sql)
                        processed += 1
        log.info(f"Processed {processed} statements")


class ImportGemeenteTask(ImportBagHTask):
    """
    Gemeente is not delivered by GOB. So we hardcode gemeente Amsterdam data
    """

    name = "gemeente"
    data = [
        ("0363", 1, "1900-01-01 00:00:00.00000+00", "1900-01-01", "", "Amsterdam", "J",)
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def process(self):
        gemeentes = [
            self.model(
                id=f"{r[0]}_{r[1]:03}",
                identificatie=r[0],
                volgnummer=r[1],
                registratiedatum=r[2],
                begin_geldigheid=r[3],
                eind_geldigheid=r[4] or None,
                naam=r[5],
                verzorgingsgebied=r[6] == "J",
            )
            for r in self.data
        ]
        self.model.objects.bulk_create(gemeentes, batch_size=100)


class ImportWoonplaatsTask(ImportBagHTask):
    name = "woonplaats"


class ImportStadsdeelTask(ImportBagHTask):
    name = "stadsdeel"


class ImportGgwGebied(ImportBagHTask):
    name = "ggw_gebied"


class ImportGgwPraktijkGebied(ImportBagHTask):
    name = "ggw_praktijkgebied"


class ImportWijkTask(ImportBagHTask):
    name = "wijk"


class ImportBuurtTask(ImportBagHTask):
    name = "buurt"


class ImportBouwblokTask(ImportBagHTask):
    name = "bouwblok"


class ImportOpenbareRuimteTask(ImportBagHTask):
    name = "openbare_ruimte"


class ImportLigplaatsTask(ImportBagHTask):
    name = "ligplaats"


class ImportStandplaatsTask(ImportBagHTask):
    name = "standplaats"


class ImportPandTask(ImportBagHTask):
    name = "pand"


class ImportVerblijfsobjectTask(ImportBagHTask):
    name = "verblijfsobject"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pandrelatiemodel = self.models["verblijfsobjectpandrelatie"]
        self.pandrelatie_table = self.pandrelatiemodel._meta.db_table
        self.pandrelatie_temp_table = f"{self.__class__.dataset}_pr_temp"
        self.pandrelatie = defaultdict(list)
        self.pandrelatie_count = 0
        self.panden = set()

    def before(self):
        super().before()
        self.panden = set(self.models["pand"].objects.values_list("id", flat=True))
        cursor = connection.cursor()
        cursor.execute(f"DROP TABLE IF EXISTS {self.pandrelatie_temp_table}")
        cursor.execute(
            f"CREATE TEMPORARY TABLE {self.pandrelatie_temp_table} AS TABLE {self.pandrelatie_table} WITH NO DATA"
        )
        self.pandrelatiemodel._meta.db_table = self.pandrelatie_temp_table

    def after(self):
        self.save_pandrelatie()
        super().after()
        cursor = connection.cursor()
        with transaction.atomic():
            cursor.execute(f"TRUNCATE {self.pandrelatie_table}")
            cursor.execute(f"INSERT INTO  {self.pandrelatie_table} SELECT * FROM {self.pandrelatie_temp_table}")
        self.pandrelatiemodel._meta.db_table = self.pandrelatie_table
        self.panden.clear()

    def gen_pand_vbo_objects(self):
            for pand_id, vbo_ids in self.pandrelatie.items():
                for vbo_id in vbo_ids:
                    id1 = f"{vbo_id}_{pand_id}"
                    yield self.pandrelatiemodel(id=id1, verblijfsobject_id=vbo_id, pand_id=pand_id)

    def save_pandrelatie(self):
        entries = self.gen_pand_vbo_objects()
        self.pandrelatiemodel.objects.bulk_create(entries, batch_size=batch.BATCH_SIZE)
        self.pandrelatie.clear()
        self.pandrelatie_count = 0

    def process_row(self, r):
        result = super().process_row(r)
        if result:
            id = result.id
            pand_identificaties = r["ligtIn:BAG.PND.identificatie"] or None
            if pand_identificaties:
                pand_identificaties = pand_identificaties.split("|")
                pand_volgnummers = r["ligtIn:BAG.PND.volgnummer"].split("|")
                for i in range(len(pand_identificaties)):
                    pand_id = create_id(
                        pand_identificaties[i], int(pand_volgnummers[i])
                    )
                    if pand_id not in self.panden:
                        log.error(
                            f"{self.name.title()} {id} has invalid pand_id {pand_id} ; skipping"
                        )
                    else:
                        self.pandrelatie[pand_id].append(id)
                        self.pandrelatie_count += 1
                        if self.pandrelatie_count >= batch.BATCH_SIZE:
                            self.save_pandrelatie()
                            log.info("saving pandrelaties")

        return result


class ImportNummeraanduidingTask(ImportBagHTask):
    name = "nummeraanduiding"


class ImportBagHJob(batch.BasicJob):
    name = "Import BAGH"

    def __init__(self, **kwargs):
        data_dir = settings.DATA_DIR
        if not os.path.exists(data_dir):
            raise ValueError("DATA_DIR not found: {}".format(data_dir))

        self.data_dir = data_dir
        # For utf-8 files SHAPE_ENCODING needs to be set.
        # noqa: E501 See : https://gis.stackexchange.com/questions/195862/preserving-special-chars-using-osgeo-ogr-driver-to-shapefile-in-python
        os.environ["SHAPE_ENCODING"] = "utf-8"

        dataset = Dataset.objects.get(name="bagh")
        self.models = {
            model._meta.model_name: model for model in dataset.create_models()
        }

    def __del__(self):
        os.environ.pop("SHAPE_ENCODING", None)

    def tasks(self):
        return [
            CreateBagHTables(),
            # no-dependencies.
            ImportGemeenteTask(models=self.models),
            ImportWoonplaatsTask(
                path=self.data_dir, models=self.models, use=["gemeente"]
            ),
            ImportStadsdeelTask(
                path=self.data_dir,
                models=self.models,
                gob_path="gebieden",
                references=["gemeente"],
            ),
            ImportGgwGebied(
                path=self.data_dir,
                models=self.models,
                gob_path="gebieden",
                references=["stadsdeel"],
            ),
            ImportGgwPraktijkGebied(
                path=self.data_dir,
                models=self.models,
                gob_path="gebieden",
                references=["stadsdeel"],
            ),
            ImportWijkTask(
                path=self.data_dir,
                models=self.models,
                gob_path="gebieden",
                references=["stadsdeel", "ggw_gebied"],
                extra_fields={"cbs_code": lambda r: r["cbsCode"], },
            ),
            ImportBuurtTask(
                path=self.data_dir,
                models=self.models,
                gob_path="gebieden",
                references=["wijk", "ggw_gebied", "stadsdeel"],
                extra_fields={"cbs_code": lambda r: r["cbsCode"], },
            ),
            ImportBouwblokTask(
                path=self.data_dir,
                models=self.models,
                gob_path="gebieden",
                references=["buurt"],
            ),
            ImportOpenbareRuimteTask(
                path=self.data_dir,
                models=self.models,
                gob_path="bag",
                references=["woonplaats"],
                extra_fields={"naam_nen": lambda r: r["naamNEN"], },
            ),
            ImportLigplaatsTask(
                path=self.data_dir,
                models=self.models,
                gob_path="bag",
                geotype="polygon",
                references=["buurt"],
            ),
            ImportStandplaatsTask(
                path=self.data_dir,
                models=self.models,
                gob_path="bag",
                geotype="polygon",
                references=["buurt"],
            ),
            ImportPandTask(
                path=self.data_dir,
                models=self.models,
                gob_path="bag",
                geotype="polygon",
            ),
            ImportVerblijfsobjectTask(
                path=self.data_dir,
                models=self.models,
                gob_path="bag",
                geotype="point",
                references=["buurt"],
                extra_fields={
                    "oppervlakte": lambda r: int_or_none(r["oppervlakte"]),
                    "verdieping_toegang": lambda r: int_or_none(
                        r["verdiepingToegang"]
                    ),
                    "hoogste_bouwlaag": lambda r: int_or_none(r["hoogsteBouwlaag"]),
                    "laagste_bouwlaag": lambda r: int_or_none(r["laagsteBouwlaag"]),
                    "aantal_kamers": lambda r: int_or_none(r["aantalKamers"]),
                    "eigendomsverhouding": lambda r: r["eigendomsverhouding"],
                    "gebruiksdoel": lambda r: r["gebruiksdoel"].split("|"),
                    "gebruiksdoel_woonfunctie": lambda r: r[
                                                              "gebruiksdoelWoonfunctie"
                                                          ]
                                                          or None,
                    "gebruiksdoel_gezondheidszorgfunctie": lambda r: r[
                                                                         "gebruiksdoelGezondheidszorgfunctie"
                                                                     ]
                                                                     or None,
                    "toegang": lambda r: r["toegang"].split("|")
                    if r["toegang"]
                    else [],
                    "redenopvoer": lambda r: r["redenopvoer"] or None,
                    "redenafvoer": lambda r: r["redenopvoer"] or None,
                    "heeftin_hoofdadres_id": lambda r: create_id(
                        r["heeftIn:BAG.NAG.identificatieHoofdadres"],
                        int_or_none(r["heeftIn:BAG.NAG.volgnummerHoofdadres"]),
                    ),
                    "heeftin_nevenadres_id": lambda r: create_ids(
                        r,
                        "heeftIn:BAG.NAG.identificatieNevenadres",
                        "heeftIn:BAG.NAG.volgnummerNevenadres",
                    ),
                },
            ),
            # large. 500.000
            ImportNummeraanduidingTask(
                path=self.data_dir,
                models=self.models,
                gob_path="bag",
                references=[
                    "ligplaats",
                    "standplaats",
                    "verblijfsobject",
                    "openbare_ruimte",
                ],
                extra_fields={
                    "huisnummer": lambda r: r["huisnummer"],
                    "huisletter": lambda r: r["huisletter"] or None,
                    "huisnummer_toevoeging": lambda r: r["huisnummertoevoeging"]
                                                       or None,
                    "postcode": lambda r: r["postcode"],
                    "type_adres": lambda r: r["typeAdres"],
                },
            ),
        ]
