# dso-import

This wil run the import for data needed in de dso-api.

For this import it can use the dynamically created models in dso-api. This

Therefore we need the dso-ai imported as package.

This can be installed with :

    pip install ../dso-api/src

Or from any other location where dso-api is located.

For the historical bag import fil;es need to be retrieved frm the GOB objectstore,

Therefore we need to set the GOB_OBJECTSTORE_PASSWORD:

    GOB_OBJECTSTORE_PASSWORD=secret

And the database url where this should be imported :

    export DATABASE_URL=postgres://dso_api:insecure@localhost:5415/dso_api

Then run the following command fromn the _src_ directory:

    python manage.py run_import bagh 
