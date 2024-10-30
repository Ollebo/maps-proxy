from minio import Minio
from flask import make_response
import os.path


S3_FILE_BUCKET = os.environ.get('AWS_S3_FILE_BUCKET')
S3_ENDPOINT_URL = os.environ.get('S3_ENDPOINT')
AWS_ACCESS_KEY_ID = os.environ.get('AWS_ACCESS_KEY_ID')
AWS_SECRET_ACCESS_KEY = os.environ.get('AWS_SECRET_ACCESS_KEY')


client = Minio(S3_ENDPOINT_URL,
    access_key=AWS_ACCESS_KEY_ID,
    secret_key=AWS_SECRET_ACCESS_KEY,
)


BUCKET_NAME = S3_FILE_BUCKET
FILE_PREFIX = "/file_cache/"

def getFile(filename):
    
    print(filename)
    
    WeHaveFile = False
    fileIsDownloaded = False
    FILE_DEST = FILE_PREFIX + filename
    if os.path.isfile(FILE_DEST):
        fileIsDownloaded = True
        WeHaveFile = True
        print("We have a local copy of the file" )


    if not fileIsDownloaded:
        try:
            #Get the file from the bucket
            client.fget_object(BUCKET_NAME, filename, FILE_DEST)
            WeHaveFile = True
            print("file downloaded")
        except:
            return "File not found"
    #
    ##Read the file
    if WeHaveFile:
        f = open(FILE_DEST, "rb")
        theFile = f.read()
        f.close()
        #set the content type
        response = make_response(theFile)

        #set the content type
        filename, file_extension = os.path.splitext(FILE_DEST)
        if file_extension == ".png":
            response.headers.set('Content-Type', 'image/png')
        elif file_extension == ".jpg": 
            response.headers.set('Content-Type', 'image/jpeg')
        elif file_extension == ".jpeg":
            response.headers.set('Content-Type', 'image/jpeg')
        elif file_extension == ".gif":
            response.headers.set('Content-Type', 'image/gif')
        elif file_extension == ".html":
            response.headers.set('Content-Type', 'text/html')
        elif file_extension == ".css":
            response.headers.set('Content-Type', 'text/css')
        elif file_extension == ".js":
            response.headers.set('Content-Type', 'text/javascript')
        elif file_extension == ".json":
            response.headers.set('Content-Type', 'application/json')
        return response

    else:
        return "File not found"
