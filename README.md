# dso-import

This wil run the import for data needed in de dso-api.

For this import it uses the dynamically created models in dso-api.

Therefore we need the dso-api imported as package.

That package can be installed with :

    pip install ../dso-api/src

Or from any other location where dso-api is located.

For the historical bag import files need to be retrieved frm the GOB objectstore,

Therefore we need to set the GOB_OBJECTSTORE_PASSWORD:

    GOB_OBJECTSTORE_PASSWORD=secret

And the database url where this should be imported :

    export DATABASE_URL=postgres://dso_api:insecure@localhost:5415/dso_api

Then run the following command fromn the _src_ directory:

    python manage.py run_import bagh 
    
Or if the tables need to be recreated manually

    python manage.py run_import bagh  --create

The historical bag import is meant to run with the acceptance or production database.

If not run with create tables will not be deleted and only the changed rows will be updated or new rows will be added.
With the exception of the  N-N table bagh_verblijfsobjectpandrelatie. That content completely replaced every time. 
