import fnmatch
import os
import xml.etree.ElementTree as ET

matches = []
titles=[]
index = 1

for root, dirnames, filenames in os.walk('./'):
    for filename in fnmatch.filter(filenames, 'gamelist.xml'):
        filePath = os.path.join(root,filename)
        systemName = filePath.split('/')[1]
        matches.append(filePath)
        tree = ET.parse(filePath)
        root = tree.getroot()
        #print("process"+filePath)
        games = root.findall("./game")
        for game in root.iter('game'):
            id = game.attrib.get('id','-')
            md5=''
            favorite=''
            title=' '
            path = ' '
            hidden=''

            if game.find('name') is not None:
                title=game.find('name').text
            else:
                continue

            if game.find('hidden') is not None:
                hidden=game.find('hidden').text

            if hidden == 'true':
                continue

            if game.find('path') is not None:
                path=game.find('path').text
            
            if game.find('md5') is not None:
                md5=game.find('md5').text
            
            if game.find('favorite') is not None:
                favorite=game.find('favorite').text

            if title is None:
                title = path

            #if favorite == 'true':
            gameTitle = ""+title+" "+systemName+" "+id+" " + md5
            titles.append(gameTitle)

            if favorite == 'true':
                #print(gameTitle)
                #print(str(index)+" | "+title+" | " + systemName + " |  "+ id + " | " + crc)
                index+=1
            #else:
                #print(str(index)+" "+systemName+" | "+title+" | "+ id)
                #index+=1
                

titles.sort()

for num,title in enumerate(titles):
    print(str(num)+" "+title)

