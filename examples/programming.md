# Programming: Python Fundamentals

## Variables and data types

A variable is a name that refers to a value. Python is dynamically typed, so a variable does not
have a fixed type; the value it refers to does. Common built-in types are int (whole numbers),
float (decimal numbers), str (text), bool (True or False), list and dict.

A list is an ordered, changeable sequence of items. A tuple is like a list but cannot be changed
after it is created. A dictionary stores key-value pairs and looks values up by key.

## Control flow and functions

An if statement runs a block of code only when its condition is true; elif and else handle the
other cases. A for loop repeats a block once for each item in a sequence. A while loop repeats as
long as its condition stays true.

A function is defined with the keyword def. It can take parameters and send a value back with
return. A function without a return statement gives back None.

## Errors and exceptions

When something goes wrong at run time, Python raises an exception. A try/except block catches an
exception so the program can handle it instead of crashing. For example, dividing by zero raises a
ZeroDivisionError. Code in a finally block runs whether or not an exception happened.
