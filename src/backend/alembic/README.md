Alembic is an extension of sqlalchemy that makes schema change and database migrations easy. It maintains and manages different versions of the database under the /version folder.

`Alembic.ini`: This file is the script alembic looks for when invoked. In order to invoke any alembic cli commands, you must be in
the directory that contains this file.

`script.py.mako`: This file is used as a template to generate new migration or revision scripts that can be found in /versions
env.py: This file is a python script that is executed whenever an alembic command is invoked. The file connects alembic to the sqlalchemy engine.

## Database
DDA local server has 2 databases, configuration database and metadata database. Configuration database contains control plane db tables, like workflow, image source, etc. Metadata databse contains tables like inference result.

## Alembic Commands
### Step 1
In order to create a new revision or change to the database, you can choose either manual way or automatic way. You can run the alembic revision command (preferably in the container where alembic is installed) depends on which database you are going to update: 




* **Manually generate**  
The following command will generate script with empty upgrade/downgrade functions. Complete the functions manaully with your corresponding changes.  

  Configuration database: ```alembic -n database_configuration revision -m "name of revision"```  
Metadata database: ```alembic -n database_metadata revision -m "name of revision"```

* **Automatically generate**  
The following command will generate the upgrade/downgrade functions for you. Review the functions manaully since sometimes it's not accurate.  
(The autogenerate process scans across all table objects within the database that is referred towards by the current database connection in use. Details about the detection of `--autogenerate` can be found [here](https://alembic.sqlalchemy.org/en/latest/autogenerate.html#what-does-autogenerate-detect-and-what-does-it-not-detect))

  Configuration database: ```alembic -n database_configuration revision --autogenerate -m "name of revision"```  
Metadata database: ```alembic -n database_metadata revision --autogenerate -m "name of revision"```


This will generate a new revision in /versions folder and update the alembic version table to maintain the ordering of the different database versions. Configuration db will be inside `/alembic/configuration_database/versions`, metadata db will be inside `/alembic/metadata_database/versions`.

### Step 2
In order to perform revisions to the database to get it up to date, you can run the following command: 

Configuration database: ```alembic -n database_configuration upgrade head```

Metadata database: ```alembic -n database_metadata upgrade head```

### Step 3
Make sure test downgrade as well, in case any rollback happened. 

Configuration database: ```alembic -n database_configuration downgrade -1```

Metadata database: ```alembic -n database_metadata downgrade -1```

If you would like to use the api versions of these commands in your code, check out https://alembic.sqlalchemy.org/en/latest/api/commands.html

