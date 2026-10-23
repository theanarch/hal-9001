-- Sets up a database for the hal-memory MCP server. Plain T-SQL: open it in SSMS and press Execute.
-- It is safe to run again later; it then updates the procedure and resets the login's password.
--
-- 1. Create an empty database for the agent, with a name of your choice
--    (SSMS: right-click Databases > New Database), and select it in the database list on the toolbar.
-- 2. Choose a login name and a password in the two lines below.
-- 3. Execute. Then use the same database, login and password in the server's connection string.
--
-- From the command line: sqlcmd -S localhost -E -C -d <your database> -i sql\setup.sql

DECLARE @LoginName     SYSNAME       = N'CHANGE_ME_login';
DECLARE @LoginPassword NVARCHAR(128) = N'CHANGE_ME_password';

-- The login name may only contain letters, digits and _, and must start with a letter.
-- The password needs at least 8 characters with three of: upper case, lower case, digits and symbols,
-- and may not contain the login name.

-- Stop before changing anything if a value is still a placeholder or the script runs in a system database.
-- SET NOEXEC ON makes SQL Server skip every following batch; the last batch turns it off again.
IF DB_ID() <= 4
BEGIN
    RAISERROR(N'Run this script in your own database, not in master: select it in the database list on the toolbar.', 16, 1);
    SET NOEXEC ON;
END
ELSE IF @LoginName LIKE N'CHANGE[_]ME%' OR @LoginPassword LIKE N'CHANGE[_]ME%'
BEGIN
    RAISERROR(N'Choose your own @LoginName and @LoginPassword at the top of setup.sql first.', 16, 1);
    SET NOEXEC ON;
END
ELSE IF LEN(@LoginName) > 64
     OR @LoginName COLLATE Latin1_General_BIN NOT LIKE '[A-Za-z]%'
     OR @LoginName COLLATE Latin1_General_BIN LIKE '%[^A-Za-z0-9_]%'
BEGIN
    RAISERROR(N'@LoginName may only contain letters, digits and _, starting with a letter.', 16, 1);
    SET NOEXEC ON;
END

-- Variables end with each batch (GO), so the login section further down reads the values from here.
EXEC sys.sp_set_session_context @key = N'LoginName', @value = @LoginName;
EXEC sys.sp_set_session_context @key = N'LoginPassword', @value = @LoginPassword;
GO

-- All tables the AI creates live in the schema ai, separate from anything else in the database.
IF SCHEMA_ID(N'ai') IS NULL
    EXEC (N'CREATE SCHEMA ai AUTHORIZATION dbo');
GO

-- Procedures remember these two settings from when they were created, and the procedure needs both on.
-- (SSMS has them on by default; sqlcmd turns QUOTED_IDENTIFIER off.)
SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
GO

-- The only way the AI can create a table. It runs with the owner's rights (EXECUTE AS OWNER), so the AI login
-- itself never needs CREATE TABLE or ALTER rights, which would also let it drop or truncate tables.
-- Every table gets the same frame: Id (primary key), the requested columns, CreatedAt, UpdatedAt and
-- DeletedAt (for soft deletes). Names and types are checked here as well as in the Python code.
CREATE OR ALTER PROCEDURE ai.CreateTable
    @TableName SYSNAME,
    @Columns   NVARCHAR(MAX)  -- JSON, e.g. [{"name": "Title", "type": "text", "required": true}]
WITH EXECUTE AS OWNER
AS
BEGIN
    SET NOCOUNT ON;

    IF @TableName IS NULL OR LEN(@TableName) > 64
       OR @TableName COLLATE Latin1_General_BIN NOT LIKE '[A-Za-z]%'
       OR @TableName COLLATE Latin1_General_BIN LIKE '%[^A-Za-z0-9_]%'
        THROW 50001, 'Invalid table name: use 1-64 letters, digits or _, starting with a letter.', 1;

    IF OBJECT_ID(N'ai.' + QUOTENAME(@TableName), N'U') IS NOT NULL
        THROW 50002, 'A table with that name already exists.', 1;

    IF (SELECT COUNT(*) FROM sys.tables WHERE schema_id = SCHEMA_ID(N'ai')) >= 50
        THROW 50003, 'Table limit reached: at most 50 tables.', 1;

    IF ISJSON(@Columns) = 0 OR LEFT(LTRIM(@Columns), 1) <> N'['
        THROW 50004, 'Columns must be a JSON array.', 1;

    DECLARE @cols TABLE (Ord INT NOT NULL, Name NVARCHAR(200) NULL, SqlType NVARCHAR(40) NULL, IsRequired BIT NOT NULL);

    INSERT INTO @cols (Ord, Name, SqlType, IsRequired)
    SELECT CAST(j.[key] AS INT),
           c.name,
           CASE c.type
               WHEN 'text'      THEN N'NVARCHAR(400)'
               WHEN 'long_text' THEN N'NVARCHAR(4000)'
               WHEN 'integer'   THEN N'INT'
               WHEN 'decimal'   THEN N'DECIMAL(18, 4)'
               WHEN 'boolean'   THEN N'BIT'
               WHEN 'date'      THEN N'DATE'
               WHEN 'datetime'  THEN N'DATETIME2(0)'
           END,
           ISNULL(c.required, 0)
    FROM OPENJSON(@Columns) AS j
    CROSS APPLY OPENJSON(j.[value]) WITH (name NVARCHAR(200) '$.name', type NVARCHAR(20) '$.type', required BIT '$.required') AS c;

    IF (SELECT COUNT(*) FROM @cols) NOT BETWEEN 1 AND 30
        THROW 50005, 'A table needs between 1 and 30 columns.', 1;

    IF EXISTS (SELECT 1 FROM @cols WHERE SqlType IS NULL)
        THROW 50006, 'Unknown column type. Use text, long_text, integer, decimal, boolean, date or datetime.', 1;

    IF EXISTS (SELECT 1 FROM @cols
               WHERE Name IS NULL OR LEN(Name) > 64
                  OR Name COLLATE Latin1_General_BIN NOT LIKE '[A-Za-z]%'
                  OR Name COLLATE Latin1_General_BIN LIKE '%[^A-Za-z0-9_]%')
        THROW 50007, 'Invalid column name: use 1-64 letters, digits or _, starting with a letter.', 1;

    IF EXISTS (SELECT 1 FROM @cols WHERE Name IN (N'Id', N'CreatedAt', N'UpdatedAt', N'DeletedAt'))
        THROW 50008, 'Id, CreatedAt, UpdatedAt and DeletedAt are added automatically.', 1;

    IF EXISTS (SELECT Name FROM @cols GROUP BY Name HAVING COUNT(*) > 1)
        THROW 50009, 'Each column name may only be used once.', 1;

    DECLARE @definitions NVARCHAR(MAX) = (
        SELECT N', ' + QUOTENAME(Name) + N' ' + SqlType + CASE WHEN IsRequired = 1 THEN N' NOT NULL' ELSE N' NULL' END
        FROM @cols
        ORDER BY Ord
        FOR XML PATH(''), TYPE
    ).value('.', 'NVARCHAR(MAX)');

    DECLARE @sql NVARCHAR(MAX) =
        N'CREATE TABLE ai.' + QUOTENAME(@TableName) + N' ('
        + N'Id INT IDENTITY(1, 1) NOT NULL PRIMARY KEY'
        + @definitions
        + N', CreatedAt DATETIME2(0) NOT NULL DEFAULT SYSUTCDATETIME()'
        + N', UpdatedAt DATETIME2(0) NULL'
        + N', DeletedAt DATETIME2(0) NULL);';

    EXEC sys.sp_executesql @sql;
END;
GO

-- The AI login -------------------------------------------------------------------------------------------------
-- What it can do:   read, insert and update rows in the ai schema's tables, and create new tables there
--                   through ai.CreateTable.
-- What it cannot:   DELETE, DROP, TRUNCATE or ALTER anything, create tables any other way,
--                   or touch anything outside the schema.
-- SQL Server enforces this itself, so it holds even if the server code had a bug.
--
-- CREATE LOGIN, CREATE USER and GRANT do not accept variables, so these statements are built as text, with
-- QUOTENAME around every name and around the password.

DECLARE @LoginName     SYSNAME       = CAST(SESSION_CONTEXT(N'LoginName') AS SYSNAME);
DECLARE @LoginPassword NVARCHAR(128) = CAST(SESSION_CONTEXT(N'LoginPassword') AS NVARCHAR(128));
DECLARE @login NVARCHAR(200) = QUOTENAME(@LoginName);
DECLARE @sql   NVARCHAR(MAX);

BEGIN TRY
    SET @sql = CASE WHEN SUSER_ID(@LoginName) IS NULL THEN N'CREATE' ELSE N'ALTER' END
        + N' LOGIN ' + @login + N' WITH PASSWORD = ' + QUOTENAME(@LoginPassword, N'''')
        + N', DEFAULT_DATABASE = ' + QUOTENAME(DB_NAME()) + N', CHECK_POLICY = ON;'
        + N' ALTER LOGIN ' + @login + N' ENABLE;';
    EXEC sys.sp_executesql @sql;

    IF USER_ID(@LoginName) IS NULL
    BEGIN
        SET @sql = N'CREATE USER ' + @login + N' FOR LOGIN ' + @login + N';';
        EXEC sys.sp_executesql @sql;
    END

    -- Schema-level rights also cover tables created later. DENY always wins over GRANT.
    SET @sql = N'GRANT SELECT, INSERT, UPDATE ON SCHEMA::ai TO ' + @login + N';'
        + N' DENY DELETE ON SCHEMA::ai TO ' + @login + N';'
        + N' GRANT EXECUTE ON OBJECT::ai.CreateTable TO ' + @login + N';';
    EXEC sys.sp_executesql @sql;
END TRY
BEGIN CATCH
    EXEC sys.sp_set_session_context @key = N'LoginPassword', @value = NULL;
    THROW;  -- e.g. a password that does not meet the policy: stop here and show the error
END CATCH

EXEC sys.sp_set_session_context @key = N'LoginPassword', @value = NULL;

IF CAST(SERVERPROPERTY('IsIntegratedSecurityOnly') AS INT) = 1
    PRINT N'WARNING: this server only allows Windows authentication, so the login cannot be used yet. '
        + N'In SSMS: right-click the server > Properties > Security > "SQL Server and Windows Authentication mode", '
        + N'then restart the SQL Server service.';

PRINT N'Done. Test the login with: sqlcmd -S localhost -U ' + @LoginName + N' -P "<password>" -d ' + DB_NAME() + N' -C -Q "SELECT 1"';
GO

SET NOEXEC OFF;
GO
