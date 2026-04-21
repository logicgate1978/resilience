import boto3

class AccessController:
    def __init__(self, citizenAccount: str, region: str, role: str, username: str, password: str):
        self.region = region

    def getServiceSession(self):
        return boto3.Session(region_name=self.region)