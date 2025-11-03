

class Car():
    def __init__(self, speed):
        self.speed = speed

class GenesisCope(Car):
    def __init__(self, speed, color):
        super().__init__(speed)
        self.color = color





car1 = Car(120)
print(car1.speed)
car2 = GenesisCope(100, "red")
print(car2.speed)
print(car2.color)