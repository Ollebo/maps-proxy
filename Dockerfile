FROM python
#Making folder for code
RUN mkdir /code
RUN mkdir /files


#Copying code to container
COPY ./code /code/


#Mak makings tools

#Setting working directory
WORKDIR /code

#Installing dependencies
RUN chmod +x /code/start.sh
RUN pip3 install -r /code/req.txt
#
##Running the code
## Set runtime interface client as default command for the container runtime
#ENTRYPOINT [ "/usr/local/bin/python", "-m", "awslambdaric" ]
## Pass the name of the function handler as an argument to the runtime
CMD [ "/code/start.sh" ]