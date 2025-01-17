# flask for creating the WSGI contained server
from flask import Flask, request, jsonify, send_from_directory, abort, redirect
# SQLAlchemy to access the database
from flask_sqlalchemy import SQLAlchemy
# serializer to transform query result to json
from sqlalchemy_serializer import SerializerMixin
# we will use os to access enviornment variables stored in the *.env files, time for delays and json for ajax-responses.
import os, time, json, random, sys, numpy as np, re
import secrets

from datetime import datetime

# since the builtin flask server is not for production, we use tornado
import tornado.httpserver
import tornado.wsgi
from tornado.websocket import WebSocketHandler
from tornado.web import FallbackHandler, Application

# add RL-A to importable 
sys.path.insert(0, '/code/RL-A/')
from TTTsolver import TicTacToeSolver, boardify
solver = TicTacToeSolver("presets/policy_p1","presets/policy_p2").solveState

# import mail stuff
from mail import sendMail, EMAIL_TEMPLATES

# initialize flask application. since all GET requests are static, we point the static_url_path to a dummy path
app=Flask(__name__, static_url_path='/_flask_static')
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = True
# bind database
app.config["SQLALCHEMY_DATABASE_URI"] =  f"postgresql://{os.environ['POSTGRES_USER']}:{os.environ['POSTGRES_PASSWORD']}@db/tictactoe"

# uncomment to show all db queries
# app.config['SQLALCHEMY_ECHO'] = True

db = SQLAlchemy(app)

# returns a boolean describing whether or not the server is using ssl
def sslEnabled():
    return "ENABLE_SSL" in os.environ and os.environ["ENABLE_SSL"].upper() == "TRUE"

# makes a move if it should, returns true if the bot made a move
def makeBotMove(gameId):
    # get the game
    game = Game.find(gameId)
    # if the game is ongoing and the bot is one of the players
    if game.getGameState() == Game.ONGOING and os.environ["BOT_USERNAME"] in [game.attacker, game.defender]:
        # find the best move
        solution = solver(game.getNumpyGameField().reshape((3,3)), "defender", False)
        # log the solution
        app.logger.info(f"found solution to board: {solution}")
        # create a move
        move = Move.fromXY({"y":solution[0], "x":solution[1]},os.environ["BOT_USERNAME"], game.gameId)
        # find out whether the game is finished or not
        # BUG: breaks sometimes, preventing the user to recieve updates
        game.determineState()
        # send the new state to all subscribers
        gameSubscriptions.broadcastState(game.gameId)
        return True
    else:
        return False

# table to store users, their password and email to
class User(db.Model, SerializerMixin):
    __tablename__ = "users"

    # columns of the table
    username=db.Column(db.String(16), primary_key=True, nullable=False)
    email=db.Column(db.String(256))
    key=db.Column(db.String(256))
    salt=db.Column(db.String(256))
    timestamp = db.Column(db.TIMESTAMP,server_default=db.text('CURRENT_TIMESTAMP'))
    # TODO: implement feature to disable email updates
    disableMail = db.Column(db.Boolean(), nullable=False, default=False)

    def __init__(self, username, email, key, salt):
        # username must me at least length 3
        if len(username) < 2:
            raise Exception("Username too short")

        # set values
        self.username=username
        self.email=email
        self.key=key
        self.salt=salt

    # determines whether or not a user is authorized, taking the username and key.
    @staticmethod
    def authorize(username, key):
        # log for debugging
        app.logger.info(username)
        app.logger.info(key)
        # get all users, take those with the same username (should be 0 or 1), filter to the key and count it.
        return db.session.query(User).filter(User.username==username).filter(User.key==key).count()==1

    # takes a request as input and returns true if the user is authorized
    @staticmethod
    def authorizeRequest(request):
        return User.authorize(request.form["username"], request.form["key"])
    
    # returns the resolt object of user objects when given its username, can be empty or length one
    # use find(username).one() to get one user, use find(username).count() to get its length
    @staticmethod
    def find(username):
        return db.session.query(User).filter(User.username==username)

    # takes a request as input and adds a user to the db with the data from the requests form
    @staticmethod
    def generateFromRequest(request):
        # create the user
        user = User(request.form["username"], request.form["email"], request.form["key"], request.form["salt"])
        # add it to the db
        db.session.add(user)
        # commit query
        db.session.commit()
        # refresh user from db
        db.session.refresh(user)
        # return user
        return user
    
    # returns an array of games played by the user, ordered descending by their id
    def getGames(self, limit, lastGameId):
        return db.session.query(Game).filter(Game.attacker == self.username).union(db.session.query(Game).filter(Game.defender==self.username)).filter(Game.gameId < lastGameId).order_by(Game.gameId.desc()).limit(limit).all()

# table to store games and their players to
class Game(db.Model, SerializerMixin):
    __tablename__ = "games"

    # columns
    gameId = db.Column(db.Integer(), primary_key=True, autoincrement="auto", name="gameid")
    gameKey = db.Column(db.String(32), nullable=True)
    attacker = db.Column(db.String(16), db.ForeignKey("users.username"), nullable=True)
    defender = db.Column(db.String(16), db.ForeignKey("users.username"), nullable=True)
    winner = db.Column(db.String(16), db.ForeignKey("users.username"), nullable=True)
    isDraw = db.Column(db.Boolean(), default=False)
    gameFinished = db.Column(db.Boolean(), default=False, nullable=False)
    started = db.Column(db.Boolean(), default=False, nullable=False)
    timestamp = db.Column(db.TIMESTAMP,server_default=db.text('CURRENT_TIMESTAMP'))
    # unique values to determine the games state
    ONGOING=0
    FINISHED=1

    def __init__(self, player, playAgainstBot = True):
        # set the attacker
        self.attacker = player if player else None
        # set the defender, None if the user doesn't want to play against the bot
        self.defender = os.environ["BOT_USERNAME"] if playAgainstBot else None
        # the game has started if the user wants to play against the bot, else we will wait for a other user to join
        self.started = True if playAgainstBot else False
        # only set key if not with an account or trying to play against a guest
        self.gameKey = hex(random.randrange(16**32))[2:] if not player or not playAgainstBot else None

    # determines the game and sets gameFinished, and winner to the appropriate values 
    def determineState(self):
        # get the board
        board = self.getNumpyGameField()
        # determine the winner, None if there is none, False if it is even, 1 or -1 if its a player
        winner = self.getWinnerOfBoard(board)
        # log it for debugging
        app.logger.info(f"determined winner: {winner}")
        # if the game was not set to be finished and there is a winner 
        if not self.gameFinished and (winner == False or winner == 1 or winner == -1):
            app.logger.info("game finished")
            # set the game to be finished
            self.gameFinished = True
            # if the game is not even, set the winner
            if(winner != False):
                self.winner = self.attacker if winner == 1 else self.defender
            else:
                # else set the game to be draw
                self.isDraw = True
            # get the players and send them a mail
            players = [User.find(self.attacker).one(), User.find(self.defender).one()]
            for player in players:
                # if the player didn't disable mails (TODO: give the user the option to disable mails)
                if not player.disableMail:
                    try:
                        # send the mail
                        sendMail(player.email, "Your game", EMAIL_TEMPLATES["gamefinished"], {
                            "attacker":self.attacker, 
                            "defender":self.defender, 
                            "gameField":[{1:"x", 0:"◻", -1:"o"}[i] for i in self.getGameField()], 
                            "winner":self.winner, 
                            "isDraw":self.isDraw, 
                            "domain":os.environ["DOMAIN"], 
                            "username": player.username, 
                            "gameId":self.idToHexString(),
                            "stateText": "won" if player.username == self.winner else "lost" if not self.isDraw else "ended the game in a draw"
                            })
                    except Exception as e:
                        # log if failed
                        app.logger.error("failed to send email to user", player)
        else:
            app.logger.info("game not finished yet")
        # commit changes
        db.session.commit()

    # transforms the game-id (int) to a hex-string
    def idToHexString(self, length=6):
        return ("0" * length + hex(self.gameId)[2:])[-6:]

    # returns all moves associated with this game
    def getMoves(self):
        return db.session.query(Move).filter(Move.gameId == self.gameId).all()

    # returns game field in one-dimensional array with {attacker:1, defender:-1, empty:0}
    def getGameField(self):
        # get the moves
        moves = self.getMoves()
        # create a field of 0's
        field = [0]*9
        # for every move, set the player in the field
        for move in moves:
            if move.player == self.attacker:
                field[move.movePosition] = 1 
            else:
                field[move.movePosition] = -1
        return field

    # returns the gamefield as np-array
    def getNumpyGameField(self):
        # create an empty np array
        field = np.empty(9, dtype="float64")
        # fill it with the gamefield
        field[:] = self.getGameField()
        return field

    # get the info of the game
    def getGameInfo(self):
        info = {}
        info["attacker"] = self.attacker
        info["defender"] = self.defender
        info["gameId"] = self.gameId
        info["winner"] = self.winner
        info["gameField"] = self.getGameField()
        info["isFinished"] = self.gameFinished
        info["isDraw"] = self.isDraw
        return info

    # gets the winner of game (string), None if draw, False if ongoing
    def getWinner(self):
        if not self.gameFinished:
            return False 
        else:
            return self.getMoves()[-1].player

    # returns state of game => Game.ONGOING | Game.FINISHED
    def getGameState(self):
        return self.ONGOING if self.getWinner() == False else self.FINISHED

    # returns a response json of the game containing the gameId and the gameKey
    def toResponse(self):
        response = {"data":{}}
        response["data"]["gameId"] = self.idToHexString()
        response["success"]=True
        if not (self.attacker and self.defender):
            response["data"]["gameKey"] = self.gameKey
        return response
    
    # figure out whether or not a user or a gamekey is allowed to make moves on this game
    def authenticate(self, username, gameKey):
        return ((self.attacker == username or self.defender == username) and not username == None) or self.gameKey == gameKey

    @staticmethod
    # lets a user join the game
    def join(request):
        # get the username (None if guest)
        username = Session.authenticateRequest(request)
        # get the game
        game = Game.findByHex(request.form["gameId"])

        # if the game has started, the user can't join, and if the user is allready playing we'll give an error aswell
        if game.started:
            raise ValueError("game allready started")
        if game.attacker == username:
            raise ValueError("can't play against yourself")

        # if we are here, the user may join
        game.started = True
        # if a username is set (not guest) we can delete the gameKey
        if username:
            game.defender = username
            game.gameKey = None

        # commit changes
        db.session.commit()
        # refresh the game
        db.session.refresh(game)
        return game

    @staticmethod
    # find a game by its id (decimal)
    def find(gameId):
        return db.session.query(Game).filter(Game.gameId == gameId).one()

    @staticmethod
    # find a game by its hex id
    def findByHex(gameId):
        return Game.find(int("0x" + gameId, 16))

    @staticmethod
    # rotate an array by 45  degrees (for getting diagonal moves in one line)
    def rotate45(array2d):
        rotated = [[] for i in range(np.sum(array2d.shape)-1)]
        for i in range(array2d.shape[0]):
            for j in range(array2d.shape[1]):
                rotated[i+j].append(array2d[i][j])
        return rotated

    @staticmethod
    # create a game with a user
    def createWithUser(username):
        # make a new game
        game = Game(username)
        # add the game to the db and refresh and return
        db.session.add(game)
        db.session.commit()
        db.session.refresh(game)
        return game
    
    @staticmethod
    # create a game from a request
    def createFromRequest(request):
        username = Session.authenticateRequest(request)
        # return a game with playAgainstBot from the form if the user is signed in, else just a game against the bot
        if username and "playAgainstBot" in request.form:
            game = Game(username, str(request.form["playAgainstBot"]).upper() == "TRUE")
        else:
            game = Game(username)
        db.session.add(game)
        db.session.commit()
        db.session.refresh(game)
        return game
    
    @staticmethod
    # @returns players number if he wins, elseif draw False else None
    def getWinnerOfBoard(board):
        app.logger.info(f"getting winner of board={board}")
        board = board.reshape((3,3))
        app.logger.info(f"getting winner of reshaped board={board}")
        app.logger.info(f"test-rotate: {np.rot90(board)}")
        # once normal, once rotated by 45 degrees and only 3rd row of that ([2:3]) (diagonal 1) and once rotated by -45 degrees (also only 3rd row) (diagonal 2)
        for i in [board, np.rot90(board, axes=(0,1)), *[Game.rotate45(j)[2:3] for j in [board, board[::-1]]]]:
            app.logger.info(f"determining winner of sub-board {i}")
            # for every line
            for j in i:
                app.logger.info(f"looking at row {j}")
                # figure out if the average is exactly the same as the first entry
                if sum(j)/len(j) == j[0] and 0 not in j:
                    return j[0]
        if 0 in board:
            return None
        return False

# table to store moves to
class Move(db.Model, SerializerMixin):
    __tablename__ = "moves"
    # columns
    gameId = db.Column(db.Integer(), db.ForeignKey("games.gameid"), nullable=False, primary_key=True, name="gameid")
    moveIndex = db.Column(db.Integer(), nullable=False, primary_key=True, name="moveindex")
    movePosition = db.Column(db.Integer(), nullable=False, name="moveposition")
    player = db.Column(db.String(16),db.ForeignKey("users.username"))
    timestamp = db.Column(db.TIMESTAMP,server_default=db.text('CURRENT_TIMESTAMP'))

    def __init__(self, gameId, movePosition, player):
        # fill stuff
        self.gameId = str(gameId)
        self.player = player if player else None
        self.moveIndex = self.getMoveIndex()
        app.logger.info(self.moveIndex)
        self.movePosition = int(movePosition)
        # check if move is valid
        self.checkValidity()

    # get the index for this move (last one +1, 0 if no last one)
    def getMoveIndex(self):
        # set the last index to -1
        index = -1
        # get all moves, order by indexes
        moves = db.session.query(Move).filter(Move.gameId == self.gameId).order_by(Move.moveIndex.desc()).all()
        # get the real index
        index = 0 if len(moves) == 0 else moves[0].moveIndex + 1
        # if the index is 9 or higher, the move can't be
        if not index < 9:
            raise ValueError("")
        return index
    
    def checkValidity(self):
        # check if the move is on the board
        if not (self.movePosition < 9 and self.movePosition >= 0):
            raise ValueError("move is outside of the field")
        # check if there is no other entry with same pos and gameid
        sameMoves = db.session.query(Move).filter(Move.gameId == self.gameId).filter(Move.movePosition == self.movePosition).all()
        if not len(sameMoves) == 0:
            raise ValueError("field is allready occupied by another move:", (self.gameId,sameMoves[0].gameId), (self.moveIndex, sameMoves[0].moveIndex),(self.movePosition,sameMoves[0].movePosition))
        # check if the player is allowed to do this move
        # if the move-index is draw it should be the attacker (moves 0,2,4,...)
        # else it shoud be the defender (moves 1,3,5,...)
        # note: if the games player is none, "bot" is the defender (played as guest, therefore attacking)
        game = db.session.query(Game).filter(Game.gameId == self.gameId).all()[0]
        if self.moveIndex % 2 == 1 and game.attacker == self.player: # draw and player is not the attacker
            raise ValueError(f"player {self.player} is not allowed to make move #{self.moveIndex}")
        if self.moveIndex % 2 == 0 and game.defender == self.player: # odd and player is not the defender
            raise ValueError(f"player {self.player} is not allowed to make move #{self.moveIndex}")
        return True

    @staticmethod
    def fromXY(coords, user, gameId):
        # 2d index to 1d => x + y*3 (counting from 0)
        app.logger.info(f"creating move from coords: {coords}, user={user}, gameId={gameId}")
        move = Move(gameId, int(coords["x"])+int(coords["y"])*3, user)
        db.session.add(move)
        db.session.commit()
        db.session.refresh(move)
        return move

# table to store sessionKeys to (=tokens). Tokens allow faster and more secure authentication since they expire after a certain time
class Session(db.Model, SerializerMixin):
    __tablename__ = "sessions"
    # columns
    sessionId = db.Column(db.Integer(), primary_key=True, autoincrement="auto")
    username = db.Column(db.String(16), db.ForeignKey("users.username"), nullable=False)
    sessionKey = db.Column(db.String(256), nullable=False, name="sessionkey", unique=True)
    sessionStart = db.Column(db.DateTime(), nullable=False, name="sessionstart", server_default=db.text('CURRENT_TIMESTAMP'))

    def __init__(self, username, sessionKey):
        self.username= username
        self.sessionKey = sessionKey
    
    @staticmethod
    # generate a token
    def generateToken(username):
        try:
            # create a session
            instance = Session(username, secrets.token_hex(256//2))
            # add to db
            db.session.add(instance)
            db.session.commit()
            return instance
        except Exception as e:
            # throw error if failed
            app.logger.error(e)
            return False

    @staticmethod
    # find a session by its key
    def find(sessionKey):
        return db.session.query(Session).filter(Session.sessionKey==sessionKey)

    @staticmethod
    # authenticate a request by the session, takes the Authorisation Bearer from the header
    def authenticateRequest(request):
        token = re.match(r"(Bearer)\ ([0-f]*)", request.headers["Authorisation"]).groups()[-1] if "Authorisation" in request.headers else None
        return Session.authenticateToken(token)

    @staticmethod
    # authenticate a token and return the username
    def authenticateToken(token):
        app.logger.info(f"authenticating token {token}")
        session = Session.find(token).one() if token else None
        return session.username if session else None

    # returns a response json from te data in the token
    def toResponse(self):
        response = {"success":True}
        response["data"] = {}
        response["data"]["token"] = self.sessionKey
        response["data"]["token_expires"] = self.getExpiration()
        response["data"]["inCompetition"] = Competition.hasJoined(self.username)
        return response

    # gets the expiration of a token
    def getExpiration(self):
        db.session.refresh(self)
        return int(os.environ["SESSION_TIMEOUT"]) + int(self.sessionStart.timestamp())


# table to store competition data to
class Competition(db.Model, SerializerMixin):
    __tablename__ = "competition"
    # columns
    username = db.Column(db.String(16), db.ForeignKey("users.username"), nullable=False, primary_key=True)
    firstName = db.Column(db.String(32), nullable=False, name="firstname")
    lastName = db.Column(db.String(32), nullable=False, name="lastname")
    age = db.Column(db.Integer(), nullable=False)
    gender = db.Column(db.String(1), nullable=False)
    timestamp = db.Column(db.DateTime(), nullable=False, name="joinedon", server_default=db.text('CURRENT_TIMESTAMP'))

    def __init__(self, user, firstName, lastName, age, gender):
        # fill data
        self.username = user.username
        self.firstName = firstName
        self.lastName = lastName
        self.age = int(age)
        self.gender = gender
        # check if the data is valid
        self.checkValidity()

    # checks if the data is valid (length of input and so on)
    def checkValidity(self):
        valid = True
        valid = valid and len(self.firstName)>0
        valid = valid and len(self.lastName)>0
        valid = valid and self.age >= 0
        valid = valid and self.age < 100
        valid = valid and self.gender in ["m", "f", "?"]
        if not valid:
            raise ValueError("invalid inputs for competition")
        return valid

    @staticmethod
    # takes a request and makes a Competition entry out of it
    def generateFromRequest(request):
        user = User.find(Session.authenticateRequest(request)).one()
        competition = Competition(user, request.form["firstName"], request.form["lastName"], request.form["age"], request.form["gender"])
        db.session.add(competition)
        db.session.flush()
        db.session.refresh(competition)
        db.session.commit()
        return competition
    
    @staticmethod
    # checks if a user has joined the competition
    def hasJoined(username):
        return db.session.query(Competition).filter(Competition.username == username).count() > 0

# list where websocket-connections are stored and can subscribe to games to recieve updates
class GameSubscriptionList:
    # subscription list: {gameId: [socket, msgId][]]}
    subscriptions = {}

    def __init__(self):
        pass

    # subscribe to a game
    def add(self, gameId, msgId, socket): 
        try:
            # if the gameId is not yet subscribed, add it with an empty array
            if not gameId in self.subscriptions:
                self.subscriptions[gameId] = []
            # add the subscription
            self.subscriptions[gameId].append((socket, msgId))
            app.logger.info(f"added socket to list of game #{gameId}")
            return True
        except Exception as e:
            app.logger.info(e)
            return False

    # unsubscribe from a game
    def remove(self, socket, gameId=False):
        try:
            if gameId:
                # BUG: the socket is not directly in the array, the elements in the array are [socket, msgId]
                while socket in self.subscriptions[gameId]:
                    self.subscriptions[gameId].remove(socket)
                app.logger.info(f"removed socket from list of game #{gameId}")
            else:
                for gameId in self.subscriptions.keys():
                    self.remove(socket, gameId)
        except Exception as e:
            return False
        return True

    # broadcast data to all subscribers
    def broadcast(self, gameId, data):
        # log
        app.logger.info(f"broadcasting data {data} to game #{gameId}", self.subscriptions[gameId])
        # for every subscriber
        for (socket, msgId) in self.subscriptions[gameId]:
            try:
                # send the data
                socket.send("broadcast", data, msgId)
            except:
                # socket is probably closed, just ignore
                pass
        # log
        app.logger.info(f"broadcasted data to game #{str(gameId)}")
        return

    # broadcasts a games data to all subscribers given a gameId
    def broadcastState(self, gameId):
        try:
            # get the games data
            data = Game.find(gameId).getGameInfo()
            # log the data
            app.logger.info(data)
            # broadcast the data
            self.broadcast(gameId, data)
        except Exception as e:
            # log the error
            app.logger.error(e)
            app.logger.error(f"failed to broadcast game {gameId}")
        return

# create a new instance of the game subscription list
gameSubscriptions = GameSubscriptionList()

# extend the WebSocketHandler class to add the gameSubscriptions list
class WebSocket(WebSocketHandler):
    # allow all origins
    def check_origin(self, origin):
        return True
 
    # on open log the connection
    def open(self):
        print("Socket opened.")

    # when a message is received, act accordingly
    def on_message(self, data):
        try:
            # decode the json
            message = data if type(data) == "dict" else json.loads(data)
            # get the action from the message
            action = message["action"] if "action" in message else None
            # get the arguments from the message
            arguments = message["args"] if "args" in message else {}
            # get the message id from the message
            msgId = message["msgId"] if "msgId" in message else None
        except:
            return self.error(None,["unparseable data",data], msgId)

        # return pong on ping
        if action == "ping":
            return self.send(action, "pong", msgId)

        # subscribe to a game
        if action == "subscribeGame":
            # verify that gameId is in arguments
            if "gameId" not in arguments:
                return self.error(action, "parameter not given: gameId", msgId, arguments)
            # try to subscribe to the game, respond with an error if it fails
            if not gameSubscriptions.add(arguments["gameId"], msgId, self):
                return self.error(action, "failed to subscribe to game", msgId, arguments)
            # respond with success
            return self.send(action, {"gameId":arguments["gameId"]}, msgId)
        
        # unsubscribe from a game
        if action == "unsubscribeGame":
            # verify that gameId is in arguments
            if "gameId" not in arguments:
                return self.error(action, "parameter not given: gameId", msgId, arguments)
            # try to unsubscribe from the game, respond with an error if it fails
            if not gameSubscriptions.remove(self, arguments["gameId"]):
                return self.error(action, "failed to unsubscribe from game", msgId, arguments)
            # respond with success
            return self.send(action, {"gameId":arguments["gameId"]}, msgId) 

        # view a games data
        if action == "viewGame":
            # verify that gameId is in arguments
            if "gameId" not in arguments:
                return self.error(action, "parameter not given: gameId", msgId, arguments)
            # find the game
            game = Game.find(arguments["gameId"])
            # get the games data
            data = game.getGameInfo()
            # send the data
            return self.send(action, data, msgId)
        
        # make a move
        if action == "makeMove":
            # verify that gameId is in arguments
            if "gameId" not in arguments:
                return self.error(action, "parameter not given: gameId", msgId, arguments)
            # get the game
            game = Game.find(arguments["gameId"])
            # get the username
            username = Session.authenticateToken(arguments["token"]) if "token" in arguments else None
            # get the gameKey
            gameKey = arguments["gameKey"] if "gameKey" in arguments else None
            # verify that a position is given
            if "movePosition" not in arguments:
                return self.error(action, "parameter not given: movePosition", msgId, arguments)
            # authenticate the user
            if not game.authenticate(username, gameKey):
                return self.error(action, "authentication failed", msgId, arguments)
            # verify that the game is not over
            if game.gameFinished:
                return self.error(action, "Game finished, no moves allowed", msgId, arguments)
            try:
                # make the move
                db.session.add(Move(game.gameId, int(arguments["movePosition"]), username))
                # commit the move
                db.session.commit()
            except Exception as e:
                app.logger.error(e)
                return self.error(action, "failed to make move", msgId, arguments)
            # respond with success
            self.send(action, {"success": True}, msgId)
            # broadcast the new game data
            gameSubscriptions.broadcastState(game.gameId)
            # re-calculate games state after commit of move
            game.determineState()
            # ask the bot to make a move
            makeBotMove(game.gameId)
            return 
        
        # get a list of all commands
        if action == "help":
            self.send(action, {"makeMove":{"args":["gameId", "movePosition", "?token", "?gameKey"], "desc":"makes a move on a certain game at the given position"}, "viewGame":{"args":["gameId"], "desc":"returns all the moves made on a certain game and its state"}, "ping":{"args":[], "desc":"returns \"pong\", use this to test the connection and its speed"}}, msgId)
            return 

        # respond with an error that the action is not recognized
        return self.error(action, "unknown action. send {\"action\"=\"help\"} to recieve docs", msgId, arguments)

    # on close unsubsribe from all subscribed games
    def on_close(self):
        gameSubscriptions.remove(self)
        print("Socket closed.")

    # send an error message to the client
    def error(self, action, data, msgId, arguments = {}):
        self.send(action, {"data":data, "args":arguments}, msgId, True)

    # send a message to the client
    def send(self, action, data, msgId, error=False):
        message = {"action":action, "success": False, "error":data, "msgId":msgId} if error else {"action":action, "success":True, "data":data, "msgId":msgId}
        self.write_message(json.dumps(message))

# set headers for the cors (used for development, but doesn't matter if present in production)
@app.after_request
def apply_caching(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "*"
    response.headers["Access-Control-Allow-Credentials"] = "true"
    return response

# serve built files (all files in the build folder)
@app.route('/', defaults={'path': 'index.html'}, methods=["GET"])
@app.route('/<path:path>', methods=["GET"])
def appLoader(path):
    # redirect to https if requested via http
    if sslEnabled() and request.scheme == "http":
        return redirect(request.url.replace("http://", "https://"))
    else:
        try:
            # serve the file
            return send_from_directory('/build/', path)
        except:
            # serve the index.html file if the file doesn't exist
            return send_from_directory('/build/', 'index.html')

# authentication

# returns the salt for a user
@app.route("/getsalt", methods=["POST"])
def getsalt():
    # returns salt for keygeneration of demanded user
    response = {"success":True}
    try:
        # find the user
        user = User.find(request.form["username"]).one()
        # put the salt in the response
        response["data"] = user.salt
    except:
        response["success"] = False
    # return the response
    return json.dumps(response)

# returns the token for a user
@app.route("/login", methods=["POST"])
def loginSubmission():
    try:
        # check if users credentials match and generate a token
        response = Session.generateToken(request.form["username"]).toResponse() if User.authorizeRequest(request) else {"success":False}
    except:
        response = {"success": False}
    # return the response
    return json.dumps(response)

# creates an account
@app.route("/signup", methods=["POST"])
def signupSubmission():
    try:
        # create user
        user = User.generateFromRequest(request)
        # generate a token
        response = Session.generateToken(user.username).toResponse()
        try:
            # try to send the user a welcome email
            sendMail(user.email, "Thanks for joining us!", EMAIL_TEMPLATES["signupconfirmation"], {"username":user.username, "domain": os.environ["DOMAIN"]})
        except Exception as e:
            # if the email fails, delete the user
            db.session.delete(user)
            # re-raise the exception
            raise Exception(e)
    except Exception as e:
        app.logger.error(e)
        response = {"success": False}
    # return the response
    return json.dumps(response)

# join the competition
@app.route("/joinCompetition", methods=["POST"])
def joinCompetition():
    try:
        # cenerate a competition entry for the user
        competition = Competition.generateFromRequest(request)
        # send an email to the user confirming the entry
        sendMail(User.find(competition.username).one().email, "Confirmation", EMAIL_TEMPLATES["joinedcompetition"], {"username":competition.username, "domain":os.environ["DOMAIN"]})
        response = {"success":True}
    except Exception as e:
        app.logger.error(e)
        response = {"success": False}
    # return the response
    return json.dumps(response)

# DEPRECATED
# @app.route("/games/<gameId>", methods=["POST"])
# def returnGameInfo(gameId):
#     response = {"success":True}
#     try:
#         game = Game.find(gameId)
#         response["game"] = game.to_dict()
#         response["moves"] = game.getMoves()
#     except Exception as e:
#         response["success"] = False
#         app.logger.error(e)
#     return json.dumps(response)

# get a list of all games
@app.route("/games", methods=["POST"])
def getGameList():
    response = {"success":True}
    # get the limit
    LIMIT = os.environ["GAMELIST_LIMIT"]
    try:
        # set an empty array
        games = []
        # get the id of the last loaded game from the request. only older games will be loaded
        lastGameId = float(request.form["gameId"] if "gameId" in request.form else "inf")
        # get the games
        for game in db.session.query(Game).filter(Game.gameId < lastGameId).order_by(Game.gameId.desc()).limit(LIMIT).all():
            # add the game to the array
            games.append(game.getGameInfo())
        # return the games
        response["data"]=games
    except Exception as e:
        response["success"]=False
        app.logger.error(e)
    # return the response
    return json.dumps(response)

# get a list of all USERS
@app.route("/users", methods=["POST"])
def getUserList():
    response = {"success": True}
    LIMIT = os.environ["GAMELIST_LIMIT"]
    try:
        # TODO: translate it to sqlalchemy-stuff later 
        # get the users and their stats
        userList = db.session.execute("""SELECT users.username, COALESCE(wincount, 0) AS wincount, COALESCE(defeatcount, 0) AS defeatcount, COALESCE(drawcount, 0) AS drawcount from users
LEFT JOIN (
    SELECT username, COUNT(games.winner) AS wincount FROM users RIGHT JOIN games ON users.username = games.attacker OR users.username = games.defender WHERE games.winner = users.username AND games."gameFinished" IS TRUE GROUP BY username
    ) as w on w.username = users.username
LEFT JOIN(
    SELECT username, COUNT(games.winner) AS defeatcount FROM users RIGHT JOIN games ON users.username = games.attacker OR users.username = games.defender WHERE games.winner != users.username AND games."gameFinished" IS TRUE GROUP BY username
    ) as d on d.username = users.username
LEFT JOIN(
    SELECT username, COUNT(games.*) AS drawcount FROM users RIGHT JOIN games ON users.username = games.attacker OR users.username = games.defender WHERE games.winner IS NULL AND games."gameFinished" IS TRUE GROUP BY username
    ) as e on e.username = users.username
    
ORDER BY wincount DESC NULLS LAST;""").all()
        # create an array of users
        userData = [{"username":user.username, "winCount":user.wincount, "defeatCount":user.defeatcount, "drawCount":user.drawcount} for user in userList]
        response["data"] = userData
    except Exception as e:
        response["success"] = False
        app.logger.error(e)
    # return the response
    return json.dumps(response)

# get all games played by a user
@app.route("/users/<username>", methods=["POST"])
def returnUserPage(username):
    response = {"success":True}
    # get the limit
    LIMIT = os.environ["GAMELIST_LIMIT"]
    try:
        # find the user
        user = User.find(username).one()
        # return with 404 if the user was not found
        if not user:
            abort(404)
        # get the games
        games = []
        lastGameId = float(request.form["gameId"] if "gameId" in request.form else "inf")
        for game in user.getGames(LIMIT, lastGameId):
            games.append(game.getGameInfo())
        response["data"]={"user":user.username, "games": games}
    except Exception as e:
        response["success"] = False
        app.logger.error(e)
    # return the response
    return json.dumps(response)

# check if the credentials are correct
@app.route("/checkCredentials", methods=["POST"])
def checkCredentials():
    try:
        # get the username from the token
        username = Session.authenticateRequest(request)
        if(username == None):
            raise Exception("invalid token")
        response = {"success": True, "data": username}
        return json.dumps(response)
    except Exception as e:
        app.logger.error(e)
        response = {"success": False}
        return json.dumps(response)

# start a new game
@app.route("/startNewGame", methods=["POST"])
def startNewGame():
    try:
        app.logger.error("starting new game")
        # create a game from the request
        game = Game.createFromRequest(request)
        # transform it to a json response
        response = game.toResponse()
        return json.dumps(response)
    except Exception as e:
        app.logger.error(e)
        response={"success": False}
        return json.dumps(response)

# join a game
@app.route("/joinGame", methods=["POST"])
def joinGame():
    try:
        # join the game
        game = Game.join(request)
        # transform it to a json response
        response = game.toResponse()
        return json.dumps(response)
    except Exception as e:
        app.logger.error(e)
        response={"success": False}
        return json.dumps(response)

# DEPRECATED: use WebSocket instead
# make a move
@app.route("/makeMove", methods=["POST"])
def makeMove():
    try:
        # find the game
        game = Game.findByHex(request.form["gameId"])
        # get the username
        username = Session.authenticateRequest(request)
        # get the gameKey
        gameKey = request.form["gameKey"] if "gameKey" in request.form else None
        # authenticate the user
        if not game.authenticate(username, gameKey):
            raise ValueError("no entries found")
        # if the game is finished, no moves can be made
        if game.gameFinished:
            raise ValueError("Game finished, no moves allowed")
        # add a new move with the data from the request to the database
        db.session.add(Move(game.gameId, int(request.form["movePosition"]), username))
        # commit the changes
        db.session.commit()
        # re-calculate games state after commit of move
        game.determineState()
        # send an update to all subscribers
        gameSubscriptions.broadcastState(game.gameId)
        # if game is not finished and bot is attacker or defender, let RL-A decide on the next move
        makeBotMove(game.gameId)
        response = {"success": True}
    except Exception as e:
        # app.logger.error(traceback.format_exc())
        response = {"success": False}
    return json.dumps(response)

# view a game
@app.route("/viewGame", methods=["POST"])
def sendGameInfo():
    response = {"success":True}
    try:
        # find the game and get its gameInfo
        response["data"] = Game.find(request.form["gameId"]).getGameInfo()
    except Exception as e:
        app.logger.error(e)
        response["success"] = False
    # return the response
    return json.dumps(response)

# get the version of the Backend
@app.route("/version", methods=["POST"])
def getVersion():
    response = {"success":True}
    try:
        # get the version from git
        remoteVersion = os.popen("git ls-remote origin -h HEAD").read().rstrip()
        # return the data and find out if the newest version is the same as the running version
        response["data"]={"versionHash":versionHash, "upToDate":remoteVersion.startswith(versionHash)}
    except Exception as e:
        response["success"] = False
    return json.dumps(response)

# for .well-known stuff (e.g. acme-challenges for ssl-certs)
# 
# note that directory traversal vulnerabilities are prevented by send_from_directory 
# as it checks if the path of the absolute file is inside the given directory (see https://tedboy.github.io/flask/_modules/flask/helpers.html#send_from_directory)
@app.route("/.well-known/<path:filename>")
def wellKnown(filename):
    return send_from_directory("/.well-known", filename)

# robots.txt (see https://www.robotstxt.org/)
@app.route("/robots.txt")
def robots():
    return "User-agent: *\nDisallow: *"


# start the server
if __name__ == "__main__":   
    # versionHash = os.popen("git rev-parse HEAD").read().rstrip()
    versionHash = "NONE"

    # add sample data for testing
    def addSampleData(dataCount=0):
        # make n users
        userList = [f"sampleUser{i}" for i in range(dataCount)]
        # add users
        for username in userList:
            db.session.add(User(username, f"emailof@{username}.localhost",secrets.token_hex(256//2),secrets.token_hex(256//2)))
        db.session.commit()
        
        # add games
        for _ in range(dataCount**2):
            # make a game with a random user
            game = Game(random.choice(userList))
            db.session.add(game)
            db.session.flush()
            db.session.refresh(game)
            db.session.commit()
            # add moves 
            for i in range(9):
                print(i,game.attacker if i % 2 == 0 else os.environ["BOT_USERNAME"])
                db.session.add(Move(game.gameId, i, game.attacker if i % 2 == 0 else os.environ["BOT_USERNAME"]))
            db.session.commit()  
        return

    # returns true if server is reachable
    def serverUp():
        try:
            db.create_all()
            print("database initialized")
            return True
        except Exception as e:
            print("Failed to initialize")
            print(e)
            return False

    app.debug = True
    
    # wait for database to be reachable before starting flask server
    print("waiting for server to start")
    while not serverUp():
        print("databaseserver unreachable, waiting 5s")
        time.sleep(5)

    # add bot-user for RL-A
    try:
        app.logger.info("adding a bot user")
        if User.find(os.environ["BOT_USERNAME"]).count() == 0:
            db.session.add(User(os.environ["BOT_USERNAME"], os.environ["BOT_EMAIL"], secrets.token_hex(256//2), secrets.token_hex(256//2)))
            db.session.commit()
            app.logger.info("bot user added")
        else:
            app.logger.info("bot user already exists")
        # print(db.session.query(User).all())
    except Exception as e:
        app.logger.info("failed to add bot user")
        pass

    try:
        print("adding sample data")
        addSampleData(0)
    except Exception as e:
        print(e)

    print("server reached and initialized, starting web-service")

    print("ssl enabled:", os.environ["ENABLE_SSL"])

    # create a WSGI container from flask
    flaskApp = tornado.wsgi.WSGIContainer(app)
    container = Application([
        # when a client requests for /ws, call the websokect handler to upgrade the connection to a websocket
        (r'/ws', WebSocket),
        # handle all other requests with flask
        (r'.*', FallbackHandler, dict(fallback=flaskApp))
    ])

    # set up a http server and start it
    http_server = tornado.httpserver.HTTPServer(container)
    http_server.listen(os.environ["HTTP_PORT"])

    if sslEnabled():
        # set up a https server and start it if it should
        https_server = tornado.httpserver.HTTPServer(container, ssl_options={
            "certfile": f"{os.environ['CERT_DIR']}/cert.pem",
            "keyfile": f"{os.environ['CERT_DIR']}/privkey.pem",
        })
        https_server.listen(os.environ["HTTPS_PORT"])

    print("servers started")
    # start an IOLoop
    tornado.ioloop.IOLoop.current().start()
    