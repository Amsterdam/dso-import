import logging
import sys

from django.core.management import BaseCommand

from dso_import.batch import batch
from dso_import.bagh.batch import ImportBagHJob

log = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Import data for dataset"
    requires_system_checks = False
    ordered = [
        "bagh",
    ]

    imports = dict(bagh=[ImportBagHJob])

    def add_arguments(self, parser):
        parser.add_argument(
            "dataset",
            nargs="*",
            default=self.ordered,
            help="Dataset to import, choose from {}".format(
                ", ".join(self.imports.keys())
            ),
        )

        parser.add_argument(
            "--bagh_start",
            nargs=1,
            default=['gemeente'],
            help="Start task for import",
        )

    def handle(self, *args, **options):
        datasets = options["dataset"]

        for one_ds in datasets:
            if one_ds not in self.imports.keys():
                log.error(f"Unkown dataset: {one_ds}")
                sys.exit(1)

        sets = [ds for ds in self.ordered if ds in datasets]  # enforce order

        start_task = options['bagh_start'][0]
        for one_ds in sets:
            for job_class in self.imports[one_ds]:
                batch.execute(job_class(), start_task)
