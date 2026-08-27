import pandas as pd
import tkinter as tk
from tkinter import ttk
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

#FUNCTION DEFINATIONS
def finalSelectedMinistry(*args):
    toDisplay = selectedMinistry.get()
    final.config(text=f"Selected Ministry : {toDisplay}")
    if toDisplay != "Select Ministry":
        instruction.config(text="")
        displayGraph(toDisplay)
        updateMinistryStats(toDisplay)

def displayGraph(toDisplay):
    print(toDisplay)
    for i in range(len(data)):
        dataRows = data.iloc[i]
        if dataRows[0] == toDisplay:
            xpoints=["2022-23","2023-24","2024-25(estimates)"]
            ypoints=[float(dataRows[1]),float(dataRows[2]),float(dataRows[3])]
            fig,graph = plt.subplots(facecolor="#C2B280")
            graph.plot(xpoints,ypoints,marker="o",linestyle="-",ms=7,mfc="y")
            graph.set_title(f"Budget allocation of {toDisplay}")
            graph.set_xlabel("Financial Years")
            graph.set_ylabel("Rupees (Crores)")
            graph.grid()

            if hasattr(root,"canvas"):
                root.canvas.get_tk_widget().destroy()
            
            root.canvas = FigureCanvasTkAgg(fig,master=root)
            root.canvas.draw()
            root.canvas.get_tk_widget().pack(side=tk.TOP,fill=tk.BOTH,expand=1)
            break

def updateMinistryStats(toDisplay):
    for i in range(len(data)):
        dataRows = data.iloc[i]
        if dataRows[0] == toDisplay:
            values=[f"2022-23 : {float(dataRows[1])} crores",f"2023-24 : {float(dataRows[2])} crores",f"2024-25 : {float(dataRows[3])} crores"]
            values="\n".join(values)
            displayMinistryStats.config(text=values)
            break

#THE MAIN DATAFRAME
data = pd.read_csv("./assets/Government Budget DATA.csv")

#CLEANING THE DATA
data = data.drop([102])
data = data.drop(["Budget Estimates 2023-2024 Total"],axis=1)
data = data[-(data == "     ...    ").any(axis=1)]

#OBTAINING ALL THE MINISTRIES FROM THE DATAFRAME
ministries = data["Particulars"]
ministryValues=[]
for i in ministries:
    ministryValues.append(i)
ministryValues.sort()
ministryValues.insert(0,"Select Ministry")

#TKINTER WINDOW
root = tk.Tk()
root.title("Budget Allocation of Different Ministries in India")
root.configure(bg="#B0E0E6")
style = ttk.Style(root)

#CREATING A FRAME
frame = ttk.Label(root,background="#B0E0E6",borderwidth=7,relief="solid")
frame.pack(fill=tk.X,expand=True)

#TKINTER WINDOW - CREATION OF DROPDOWN MENU
instruction = ttk.Label(frame,text="Select ministry from the dropdown menu:",background="#B0E0E6",font=('Times New Roman',16))
instruction.pack(pady=10)

selectedMinistry = tk.StringVar()
dropDown = ttk.OptionMenu(frame,selectedMinistry,*ministryValues)
dropDown.pack(pady=10)
style.configure("TMenubutton",background="#B0E0E6",font=('Times New Roman',16))
selectedMinistry.trace("w",finalSelectedMinistry)

final = ttk.Label(frame,text="Selected Ministry : None",background="#B0E0E6",font=('Times New Roman',16))
final.pack(pady=10)

#DISPLAYING MINISTRY DETAILS
displayMinistryStats = ttk.Label(frame,text="Selected Ministry Stats : NA",font=("Times New Roman",16),background="#B0E0E6")
displayMinistryStats.pack(pady=10)

root.attributes("-fullscreen",True)
root.mainloop()